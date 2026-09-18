"""Shared, host-local command inspection. No unsandboxed execution fallback.

Shell syntax is unrestricted; read-only mounts, a private PID namespace and a
seccomp network filter enforce the boundary instead of a command-name allowlist.
Formal workflow execution/publication remains owned by Execute and CommitStep.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import platform
import re
import selectors
import shutil
import signal
import stat
import socket
import struct
import subprocess
import sys
import tempfile
import time
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from agent_core.process_registry import ACTIVE_PROCESS_GROUPS


class CommandInspectionError(ValueError):
    pass


def command_observation_for_model(result: Mapping[str, Any]) -> dict[str, Any]:
    """Keep inspection evidence in model context; detailed telemetry stays local.

    The durable receipt and F2/profiler still retain the full scan roots/timings.
    Those repeated operational details must not consume Recovery's observation
    budget or displace the stdout/stderr needed for a decision.
    """
    return {key: value for key, value in result.items()
            if key not in {'timing', 'activity_id'}}


def command_message_for_model(message):
    """Project only known framework command ToolMessage shapes, not business JSON.

    stdout/stderr and arbitrary reader bodies are opaque, even if they contain
    a field named timing/read_paths. Original archives/messages stay unchanged.
    """
    from langchain_core.messages import ToolMessage
    if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
        return message
    try:
        value = json.loads(message.content)
    except (ValueError, TypeError):
        return message
    if not isinstance(value, dict):
        return message
    changed = False
    if value.get("schema_version") == 1 and value.get("request_id") and value.get("tool") in {"exec.command", "inspect.paths"} and isinstance(value.get("result"), dict):
        projected = command_observation_for_model(value["result"])
        changed = projected != value["result"]
        if changed:
            value = {**value, "result": projected}
            import hashlib
            value["content_sha256"] = hashlib.sha256(json.dumps(projected, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    elif message.name in {"exec_command", "inspect_paths"} and value.get("tool") in {"exec_command", "inspect_paths"}:
        projected = command_observation_for_model(value)
        changed = projected != value; value = projected
    return message.model_copy(update={"content": json.dumps(value, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"))}) if changed else message


@contextmanager
def _tracked_process(process):
    ACTIVE_PROCESS_GROUPS.add(process.pid)
    try:
        yield
    finally:
        try:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        finally:
            ACTIVE_PROCESS_GROUPS.discard(process.pid)


def _directory(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise CommandInspectionError(f"Not a directory: {path}")
    if path in {Path('/'), Path('/home'), Path('/public'), Path('/public/home'),
                Path('/tmp'), Path('/etc'), Path('/proc'), Path('/root'), Path.home().resolve()}:
        raise CommandInspectionError(f"Choose a project directory, not a shared/home root: {path}")
    return path


@dataclass(frozen=True)
class ProjectContext:
    project_dir: Path
    startup_dir: Path
    source: str
    explicit: bool = False

    @classmethod
    def discover(cls, project_dir=None, *, startup_dir=None, environ=None):
        env = os.environ if environ is None else environ
        startup = Path(startup_dir or Path.cwd()).resolve()
        configured = project_dir or env.get('BIOMIND_PROJECT_DIR')
        if configured:
            candidate = Path(configured).expanduser()
            if not candidate.is_absolute():
                candidate = startup / candidate
            return cls(_directory(candidate), startup, 'configured', True)
        for parent in (startup, *startup.parents):
            if (parent / '.biomind-project').exists() or (parent / '.git').exists():
                try:
                    return cls(_directory(parent), startup, 'project_marker')
                except CommandInspectionError:
                    break
        return cls(_directory(startup), startup, 'startup_directory')


def _network_filter() -> bytes:
    """Classic BPF for bwrap --seccomp (no libseccomp/third-party dependency).

    Deny socket creation and io_uring (which can otherwise issue network ops).
    Reject other syscall ABIs, including x32, rather than interpreting wrong IDs.
    """
    machine = platform.machine().lower()
    if machine in {'x86_64', 'amd64'}:
        arch, denied = 0xC000003E, (41, 53, 101, 250, 298, 321, 323, 425)
    elif machine in {'aarch64', 'arm64'}:
        arch, denied = 0xC00000B7, (198, 199, 117, 219, 241, 280, 282, 425)
    else:
        raise CommandInspectionError(f'COMMAND_SANDBOX_UNSUPPORTED_ARCH: {machine}')
    instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, arch), (0x06, 0, 0, 0x80000000),
                    (0x20, 0, 0, 0), (0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x00050001)]
    for syscall in denied:
        instructions.extend(((0x15, 0, 1, syscall), (0x06, 0, 0, 0x00050001)))
    instructions.append((0x06, 0, 0, 0x7FFF0000))
    return b''.join(struct.pack('HBBI', *instruction) for instruction in instructions)


_PRIVATE_NAMES = ('.env', '.env.*', '.ssh', '.aws', '.azure', '.kube', '.gnupg',
                  '.netrc', '.pypirc', '.npmrc', '.condarc', '*credentials*', '*.pem',
                  '*.key', 'id_rsa*', 'id_ed25519*', '.git')


_PRIVATE_PATTERN = re.compile('|'.join(fnmatch.translate(pattern) for pattern in _PRIVATE_NAMES))


def _private_paths(roots, *, recursive=True, check=None, metrics=None):
    """Inspect names only. Never open credential files or follow directory links."""
    for root in roots:
        started = time.monotonic()
        if metrics is not None:
            metrics['scans'].append({'root': str(root), 'recursive': recursive})
        try:
            for directory, dirs, files in os.walk(root, followlinks=False):
                if check:
                    check()
                if metrics is not None:
                    metrics['directories'] += 1
                    metrics['entries'] += len(dirs) + len(files)
                for name in list(dirs) + files:
                    if _PRIVATE_PATTERN.fullmatch(name.lower()):
                        path = Path(directory) / name
                        yield path
                        if name in dirs:
                            dirs.remove(name)
                if not recursive:
                    break
        finally:
            # A cancelled/timed-out scan still consumed preparation time.
            if metrics is not None:
                metrics['scan_seconds'] += time.monotonic() - started


def _readonly_mounts(root: Path, hidden: set[Path]) -> list[str]:
    """Mirror only directories containing excluded entries; never open a secret.

    Overlaying a secret *after* binding its parent can fail under a surrounding
    filesystem policy. Excluding it before binding also avoids that transient
    exposure and preserves ordinary symlinks without granting their targets.
    """
    excluded = {p for p in hidden if p == root or p.is_relative_to(root)}
    if not excluded:
        return ['--ro-bind', str(root), str(root)]
    argv = ['--tmpfs', str(root)]
    if root not in excluded:
        for child in sorted(root.iterdir()):
            if child in excluded:
                continue
            if child.is_symlink():
                argv += ['--symlink', os.readlink(child), str(child)]
            elif child.is_dir() and any(p.is_relative_to(child) for p in excluded):
                argv += _readonly_mounts(child, excluded)
            else:
                argv += ['--ro-bind', str(child), str(child)]
    argv += ['--remount-ro', str(root)]
    return argv


def _runtime_locations():
    """Discover this host, never reuse a serialized server-specific conda path."""
    prefixes = {Path(sys.prefix).resolve()}
    conda = os.environ.get('CONDA_EXE') or shutil.which('conda') or ''
    if conda and Path(conda).is_file():
        conda = str(Path(conda).resolve())
        prefixes.add(Path(conda).parent.parent)
    else:
        conda = ''
    active = os.environ.get('CONDA_PREFIX')
    if active and Path(active).is_dir():
        prefixes.add(Path(active).resolve())
    registry = Path.home() / '.conda' / 'environments.txt'
    if registry.is_file():
        # This file is conda's public prefix registry, not its configuration.
        try:
            registered = registry.read_text(encoding='utf-8').splitlines()[:256]
        except OSError:
            registered = []
        for line in registered:
            if line.strip() and Path(line.strip()).is_dir():
                prefixes.add(Path(line.strip()).resolve())
    for prefix in list(prefixes):
        envs = prefix / 'envs'
        if envs.is_dir():
            try:
                prefixes.update(p.resolve() for p in envs.iterdir() if (p / 'conda-meta').is_dir())
            except OSError:
                continue
    bins = []
    for item in [str(Path(sys.executable).parent), *os.environ.get('PATH', '').split(os.pathsep), '/usr/bin', '/bin']:
        if item and Path(item).is_absolute() and Path(item).is_dir():
            resolved = os.path.abspath(item)
            if resolved not in bins:
                bins.append(resolved)
    return prefixes, conda, bins


class CommandExecutionService:
    """One service shared by all phases; every observation has a durable receipt."""

    def __init__(self, *, data_path, project_dir=None, startup_dir=None,
                 read_roots=(), deny_paths=(), bwrap_path=None):
        self.context = ProjectContext.discover(project_dir, startup_dir=startup_dir)
        Path(data_path).expanduser().mkdir(parents=True, exist_ok=True)
        self.data_path = _directory(data_path)
        self.install_dir = Path(__file__).resolve().parents[1]
        configured_roots = os.environ.get('BIOMIND_COMMAND_READ_ROOTS', '').split(os.pathsep)
        self.extra_roots = tuple(_directory(p) for p in (*read_roots, *configured_roots) if p)
        configured_denies = os.environ.get('BIOMIND_COMMAND_DENY_PATHS', '').split(os.pathsep)
        self.deny_paths = tuple(Path(p).expanduser().absolute() for p in (*deny_paths, *configured_denies) if p)
        self.bwrap = bwrap_path or shutil.which('bwrap', path='/usr/bin:/bin') or ''
        self.prefixes, self.conda, self.path = _runtime_locations()
        self._static_key = None
        self._static_argv = ()
        self._runtime_signature = self._environment_signature()
        active = os.environ.get('CONDA_PREFIX', '')
        self.active_conda_prefix = str(Path(active).resolve()) if active and Path(active).is_dir() else ''

    @staticmethod
    def _environment_signature():
        registry = Path.home() / '.conda' / 'environments.txt'
        try:
            info = registry.stat()
            identity = (info.st_mtime_ns, info.st_size, info.st_ino)
        except OSError:
            identity = None
        return (tuple(os.environ.get(k, '') for k in
                      ('PATH', 'CONDA_EXE', 'CONDA_PREFIX', 'JAVA_HOME')), identity)

    def _refresh_runtime(self):
        signature = self._environment_signature()
        if signature != self._runtime_signature:
            self.prefixes, self.conda, self.path = _runtime_locations()
            active = os.environ.get('CONDA_PREFIX', '')
            self.active_conda_prefix = os.path.abspath(active) if active else ''
            self._runtime_signature = signature

    def path_context(self, task_id=''):
        """Project-relative typed paths from the existing TaskSpec, not a registry.

        No filesystem search or existence claims. Future outputs stay declarative.
        Reload on each invocation so plan/recovery revisions cannot use stale paths.
        """
        project = self.project_for_task(task_id)
        state_file = self._task_dir(task_id).parent / 'plan_state.json'
        try:
            state = json.loads(state_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            state = {}
        spec = (state.get('current_task') or {}).get('task_spec') or {}
        paths = []
        for field in ('inputs', 'output_contracts'):
            for row in spec.get(field) or []:
                if not isinstance(row, dict):
                    continue
                value = row.get('canonical_path') or row.get('path')
                if not isinstance(value, str) or not value or '{' in value:
                    continue
                # A plan-relative filename has not acquired its producer's path yet.
                if field == 'output_contracts' and not Path(value).is_absolute():
                    continue
                absolute = os.path.abspath(project / Path(value).expanduser())
                paths.append({'path': absolute, 'direction': 'input' if field == 'inputs' else 'output',
                              'artifact_id': row.get('artifact_id') or row.get('output_id'),
                              'role': row.get('role'), 'path_policy': row.get('path_policy'),
                              'existence': 'not_checked'})
        return {'project_dir': str(project), 'task_revision': spec.get('revision'),
                'paths': paths, 'authority': 'existing TaskSpec; no additional approval'}

    def _task_dir(self, task_id):
        name = str(task_id or 'interactive')
        if not re.fullmatch(r'[\w.-]{1,160}', name) or name in {'.', '..'}:
            raise CommandInspectionError('Invalid task_id for command observations')
        return self.data_path / 'task_outputs' / name / 'command_inspection'

    def project_for_task(self, task_id) -> Path:
        directory = self._task_dir(task_id)
        binding = directory / 'project_context.json'
        project = self.context.project_dir
        if binding.is_file() and not self.context.explicit:
            saved = json.loads(binding.read_text(encoding='utf-8'))
            try:
                project = _directory(saved['project_dir'])
            except (OSError, ValueError, KeyError) as exc:
                raise CommandInspectionError('PROJECT_REBIND_REQUIRED: start with --project-dir or BIOMIND_PROJECT_DIR') from exc
        if not binding.is_file() or (self.context.explicit and json.loads(binding.read_text()).get('project_dir') != str(project)):
            directory.mkdir(parents=True, exist_ok=True)
            value = {'project_dir': str(project), 'startup_dir': str(self.context.startup_dir),
                     'source': self.context.source, 'hostname': socket.gethostname()}
            temporary = binding.with_name(f'.context-{uuid.uuid4().hex}.tmp')
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(binding)
        return project

    def describe(self, task_id='') -> dict[str, Any]:
        self._refresh_runtime()
        return {'project_dir': str(self.project_for_task(task_id)),
                'startup_dir': str(self.context.startup_dir), 'hostname': socket.gethostname(),
                'read_roots': [str(p) for p in (self.data_path, self.install_dir, *self.extra_roots)],
                'conda_prefixes': sorted(str(p) for p in self.prefixes if (p / 'conda-meta').is_dir()),
                'host_conda_prefix': self.active_conda_prefix,
                'python_executable': sys.executable,
                'sandbox': 'read_only; private_tmp; network_disabled',
                'command_log_dir': str(self._task_dir(task_id)),
                'bwrap_installed': bool(self.bwrap),
                'path_context': self.path_context(task_id),
                'visibility': 'cwd immediate entries and typed task files; read_paths explicitly selects subtrees; unmounted is not missing'}

    def recent_observations(self, task_id='', *, max_records=12, max_chars=16000):
        """Bounded historical evidence shared across phases, never a command cache.

        Read only this task's receipts. A project rebind or different host must
        not silently import observations from a different execution environment.
        Arbitrary shell commands cannot be automatically checked for freshness.
        """
        project = str(self.project_for_task(task_id))
        root = self._task_dir(task_id)
        candidates = []
        for directory in root.iterdir():
            if not re.fullmatch(r'[0-9a-f]{32}', directory.name) or directory.is_symlink():
                continue
            path = directory / 'receipt.json'
            try:
                if not path.is_symlink() and path.is_file():
                    stat = path.stat()
                    if stat.st_size <= 262144:
                        candidates.append((stat.st_mtime, path))
            except OSError:
                continue
        records, used = [], 0
        for modified, path in sorted(candidates, reverse=True)[:32]:
            if len(records) >= max_records:
                break
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(value, dict) or value.get('project_dir') != project:
                    continue
                if value.get('hostname') != socket.gethostname() or value.get('tool') not in {'exec_command', 'inspect_paths'}:
                    continue
                fields = ('tool', 'paths', 'stage', 'status', 'cmd', 'cwd', 'project_dir', 'hostname',
                          'environment_scope', 'exit_code', 'stdout', 'stderr', 'error',
                          'timed_out', 'truncated', 'log_refs', 'duration_seconds')
                record = {key: value[key] for key in fields if key in value}
                record['receipt_ref'] = str(path)
                record['observed_at'] = value.get('observed_at') or datetime.fromtimestamp(modified, timezone.utc).isoformat()
                record['age_seconds'] = max(0, round(time.time() - modified))
                record['context_truncated'] = {}
                for field, cap in (('cmd', 1600), ('stdout', 3000), ('stderr', 1000)):
                    content = record.get(field)
                    if isinstance(content, str) and len(content) > cap:
                        record[field] = content[:cap]
                        record['context_truncated'][field] = True
                size = len(json.dumps(record, ensure_ascii=False))
                if used + size + 512 > max_chars:
                    continue
                records.append(record)
                used += size
            except (OSError, ValueError, TypeError):
                continue
        return {'freshness': 'historical_not_revalidated', 'observations': list(reversed(records)),
                'omitted_records': max(0, len(candidates) - len(records))}

    def _argv(self, *, project, cwd, cmd, filter_fd, registry_fd, read_paths=None,
              task_paths=(), metrics=None, check=None):
        if not self.bwrap or not Path(self.bwrap).is_file():
            raise CommandInspectionError('COMMAND_SANDBOX_UNAVAILABLE: install bubblewrap on this Linux host')
        if sys.platform != 'linux':
            raise CommandInspectionError('COMMAND_SANDBOX_UNSUPPORTED_OS: use a Linux host/container')
        metrics = metrics if metrics is not None else {'scans': [], 'scan_seconds': 0., 'directories': 0, 'entries': 0}
        check = check or (lambda: None)
        allowed = {project, self.data_path, self.install_dir, *self.extra_roots}
        runtime = set(self.prefixes) - {Path('/usr'), Path('/usr/local')}
        runtime.update(Path(p) for p in self.path if not p.startswith(('/usr/', '/bin', '/sbin')))
        runtime = {p for p in runtime if p.is_dir()}
        runtime = {p for p in runtime if not any(p != q and p.is_relative_to(q) for q in runtime)}
        system = [Path(p) for p in ('/usr', '/bin', '/sbin', '/lib', '/lib64') if Path(p).exists()]
        boundaries = allowed | runtime | set(system)
        denies = set(self.deny_paths)
        denies |= {p.resolve() for p in denies}

        def admit(path):
            lexical = Path(os.path.abspath(path))
            # Resolve for authorization only, preserve the invocation spelling.
            real = lexical.resolve()
            for value in (lexical, real):
                if any(_PRIVATE_PATTERN.fullmatch(part.lower()) for part in value.parts):
                    raise CommandInspectionError(f'COMMAND_PRIVATE_PATH: {lexical}')
                if any(value == d or value.is_relative_to(d) for d in denies):
                    raise CommandInspectionError(f'COMMAND_DENIED_PATH: {lexical}')
                if not any(value == r or value.is_relative_to(r) for r in boundaries):
                    raise CommandInspectionError(f'COMMAND_PATH_OUTSIDE_READ_ROOTS: {lexical}')
            return lexical

        admit(cwd)
        # No recursive project discovery. Default is a shallow view plus exact
        # typed files. Explicit directory queries retain live recursive masking.
        selected = []
        for value in read_paths or []:
            selected.append(admit(project / Path(value).expanduser()))
        for row in task_paths:
            path = admit(Path(row['path']))
            if path.is_file():
                selected.append(path)
        selected.extend(admit(p.resolve()) for p in list(selected) if p.is_symlink())
        selected = sorted(set(selected), key=lambda p: len(p.parts))
        selected = [p for p in selected if not any(p != q and p.is_relative_to(q) and q.is_dir() for q in selected)]
        recursive = [p for p in selected if p.is_dir() and not any(p == r or p.is_relative_to(r) for r in runtime | set(system))]
        # Inspect only ancestors of selected paths, without recursing through siblings.
        shallow = {cwd}
        for path in selected:
            parent = path.parent
            while any(parent == r or parent.is_relative_to(r) for r in allowed):
                shallow.add(parent)
                if parent in allowed:
                    break
                parent = parent.parent
        shallow = {p for p in shallow if p.is_dir() and not any(p == r or p.is_relative_to(r) for r in recursive)}
        private = set(_private_paths(recursive, check=check, metrics=metrics))
        private.update(_private_paths(shallow | runtime, recursive=False, check=check, metrics=metrics))
        hidden = private | denies
        hidden |= {p.resolve() for p in hidden if p.is_symlink()}
        for p in selected:
            if any(p == h or p.is_relative_to(h) or p.resolve() == h or p.resolve().is_relative_to(h) for h in hidden):
                raise CommandInspectionError(f'COMMAND_PRIVATE_PATH: {p}')
        key = (self.bwrap, tuple(map(str, system)), tuple(self.path), self.conda,
               self.active_conda_prefix, os.environ.get('JAVA_HOME', ''))
        reused = key == self._static_key
        if not reused:
            static = [self.bwrap, '--unshare-user', '--unshare-ipc', '--unshare-pid', '--unshare-uts',
                      '--die-with-parent', '--cap-drop', 'ALL', '--tmpfs', '/tmp',
                      '--dir', '/tmp/biomind-home', '--dir', '/tmp/biomind-home/.conda']
            for root in system:
                static += ['--ro-bind', str(root.resolve()), str(root)]
            for path in ('/etc/ld.so.cache', '/etc/localtime'):
                if Path(path).is_file():
                    static += ['--ro-bind', path, path]
            self._static_key, self._static_argv = key, tuple(static)
        metrics['static_config_reused'] = reused
        argv = list(self._static_argv) + ['--seccomp', str(filter_fd)]
        # Empty ancestor directories give a navigable view without binding any
        # unselected subtree. Current entries are enumerated afresh, never cached.
        shallow_mounts = {p for p in shallow if not any(p != q and p.is_relative_to(q) for q in shallow)}
        for root in sorted(shallow, key=lambda p: len(p.parts)):
            if any(root == r or root.is_relative_to(r) for r in runtime | set(system)):
                continue
            check()
            argv += ['--tmpfs' if root in shallow_mounts else '--dir', str(root)]
            for child in sorted(root.iterdir()):
                check()
                if child in hidden:
                    continue
                if child.is_symlink():
                    argv += ['--symlink', os.readlink(child), str(child)]
                elif child.is_dir():
                    argv += ['--dir', str(child)]
                elif child.is_file():
                    argv += ['--ro-bind', str(child), str(child)]
        for root in sorted(runtime):
            check()
            argv += _readonly_mounts(root, hidden)
        for path in selected:
            check()
            if any(path == r or path.is_relative_to(r) for r in runtime | set(system)):
                continue
            if not path.exists():
                continue  # Missing exact path is not fabricated or replaced by another input.
            if path.is_symlink():
                # The lexical link was mirrored above; its authorized target is
                # separately selected, without changing the invocation path.
                continue
            argv += _readonly_mounts(path, hidden)
        for root in shallow_mounts:
            if not any(root == r or root.is_relative_to(r) for r in runtime | set(system)):
                argv += ['--remount-ro', str(root)]
        # Namespace root/placeholder directories must also be read-only.
        argv += ['--proc', '/proc', '--dev', '/dev', '--remount-ro', '/',
                 '--ro-bind-data', str(registry_fd), '/tmp/biomind-home/.conda/environments.txt',
                 '--chdir', str(cwd)]
        env = {'PATH': ':'.join(self.path), 'HOME': '/tmp/biomind-home', 'TMPDIR': '/tmp',
               'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONNOUSERSITE': '1', 'CONDARC': '/dev/null', 'CONDA_NO_PLUGINS': 'true',
               'BIOMIND_PROJECT_DIR': str(project)}
        if self.conda:
            env['CONDA_EXE'] = self.conda
        if self.active_conda_prefix:
            env['CONDA_PREFIX'] = self.active_conda_prefix
        java = os.environ.get('JAVA_HOME', '')
        if java and Path(java).is_dir():
            env['JAVA_HOME'] = str(admit(Path(java)))
        for key, value in sorted(env.items()):
            argv += ['--setenv', key, value]
        metrics['read_paths'] = list(map(str, selected))
        return argv + ['--', '/bin/bash', '--noprofile', '--norc', '-c', cmd]

    @staticmethod
    def _execute(argv, fds, timeout_seconds, cancelled, metrics=None):
        """Drain both pipes without unlimited buffering, including startup/exit.

        The worker owns Popen and its cleanup even if the async caller cancels.
        start_new_session gives the process tree a group separate from BioMind.
        """
        retained = {'stdout': bytearray(), 'stderr': bytearray()}
        sizes = {'stdout': 0, 'stderr': 0}
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        spawn_started = time.monotonic()
        with subprocess.Popen(argv, env={}, cwd='/', stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              start_new_session=True, close_fds=True, pass_fds=fds) as process, _tracked_process(process):
            if metrics is not None:
                metrics['spawn_seconds'] = time.monotonic() - spawn_started
                metrics['process_started_at'] = datetime.now(timezone.utc).isoformat()
            with selectors.DefaultSelector() as selector:
                for name, stream in (('stdout', process.stdout), ('stderr', process.stderr)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                killed = False
                while selector.get_map() or process.poll() is None:
                    if not killed and (cancelled.is_set() or time.monotonic() >= deadline):
                        timed_out = not cancelled.is_set()
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        killed = True
                    for key, _events in selector.select(timeout=0.05):
                        chunk = os.read(key.fileobj.fileno(), 16384)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        name = key.data
                        sizes[name] += len(chunk)
                        remaining = max(0, 1048576 - len(retained[name]))
                        retained[name].extend(chunk[:remaining])
                process.wait()
        return process.returncode, retained, sizes, timed_out

    @staticmethod
    def _record_metrics(task_id, result):
        # Reuse an active profiler; standalone inspection needs no profiler setup.
        module = sys.modules.get('execution.profiling')
        try:
            profiler = module.get_current_profiler() if module is not None else None
            if profiler is not None:
                profiler.record_inspection(task_id, result)
        except Exception as exc:
            result['metrics_observation_error'] = type(exc).__name__

    async def inspect_paths(self, arguments, *, task_id='', stage=''):
        """Fixed metadata operation: no arbitrary shell, file content or writes.

        Exact independent stat calls share project/runtime policy setup; duplicates
        in this request are coalesced. Results are never reused across requests.
        """
        from agent_core.cli_display import emit
        started = time.monotonic()
        requested = arguments.get('paths') if isinstance(arguments, Mapping) else None
        if (not isinstance(requested, list) or not 1 <= len(requested) <= 64
                or set(arguments) != {'paths'} or any(not isinstance(p, str) or not p or '\x00' in p for p in requested)):
            return {'tool': 'inspect_paths', 'status': 'not_run', 'error': 'INVALID_PATH_LIST'}
        self._refresh_runtime()
        project = self.project_for_task(task_id)
        allowed = {project, self.data_path, self.install_dir, *self.extra_roots, *self.prefixes}
        denied = set(self.deny_paths) | {p.resolve() for p in self.deny_paths}
        paths = list(dict.fromkeys(os.path.abspath(project / Path(p).expanduser()) for p in requested))
        identity = 'stat-' + uuid.uuid4().hex
        rows = {}
        prepare_seconds = time.monotonic() - started
        emit('activity', f'{stage} · 定点核对 {len(paths)} 个路径', purpose='status',
             task_id=task_id, node=stage, call_id=identity, progress_phase='executing')

        def inspect(value):
            begin = time.monotonic()
            path = Path(value)
            try:
                def admit(candidate):
                    if (any(_PRIVATE_PATTERN.fullmatch(part.lower()) for part in candidate.parts)
                            or any(candidate == d or candidate.is_relative_to(d) for d in denied)):
                        raise CommandInspectionError('COMMAND_PRIVATE_PATH')
                    if not any(candidate == root or candidate.is_relative_to(root) for root in allowed):
                        raise CommandInspectionError('COMMAND_PATH_OUTSIDE_READ_ROOTS')
                admit(path)
                real = path.resolve(strict=True)
                admit(real)
                info = path.stat()
                row = {'path': value, 'status': 'observed', 'exists': True,
                       'type': 'file' if stat.S_ISREG(info.st_mode) else 'directory' if stat.S_ISDIR(info.st_mode) else 'other',
                       'size': info.st_size, 'mtime_ns': info.st_mtime_ns, 'symlink': path.is_symlink()}
            except FileNotFoundError:
                row = {'path': value, 'status': 'missing', 'exists': False}
            except (OSError, ValueError) as exc:
                row = {'path': value, 'status': 'unverified', 'error': type(exc).__name__ + ': ' + str(exc)}
            row['duration_seconds'] = time.monotonic() - begin
            return row

        semaphore = asyncio.Semaphore(2)
        async def one(value):
            async with semaphore:
                rows[value] = await asyncio.to_thread(inspect, value)
        workers = [asyncio.create_task(one(value)) for value in paths]
        status, cancellation = 'completed', None
        try:
            pending = set(workers)
            while pending:
                _, pending = await asyncio.wait(pending, timeout=.05)
                if time.monotonic() - started > 30:
                    status = 'timed_out'
                    break
        except asyncio.CancelledError as exc:
            cancellation, status = exc, 'cancelled'
        finally:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        result = {'tool': 'inspect_paths', 'observation_only': True, 'status': status,
                  'hostname': socket.gethostname(), 'project_dir': str(project), 'stage': stage,
                  'environment_scope': 'fixed_metadata_only_no_shell_no_content',
                  'observed_at': datetime.now(timezone.utc).isoformat(),
                  'paths': [rows.get(p, {'path': p, 'status': 'unverified'}) for p in paths],
                  'requested_count': len(requested), 'unique_count': len(paths), 'max_concurrency': 2,
                  'timing': {'prepare_seconds': prepare_seconds, 'scan_seconds': 0, 'scans': [],
                             'command_seconds': time.monotonic()-started-prepare_seconds},
                  'duration_seconds': time.monotonic()-started}
        directory = self._task_dir(task_id) / uuid.uuid4().hex
        directory.mkdir(parents=True)
        result['receipt_ref'] = str(directory / 'receipt.json')
        self._record_metrics(task_id, result)
        (directory / 'receipt.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        emit('details', json.dumps(result, ensure_ascii=False), task_id=task_id, node=stage, purpose='telemetry')
        emit('activity', f'{stage} · 路径核对{status} · {result["duration_seconds"]:.1f}s',
             purpose='status', task_id=task_id, node=stage, call_id=identity, progress_phase=status)
        if cancellation:
            raise cancellation
        return result

    async def run(self, arguments: Mapping[str, Any], *, task_id='', stage='') -> dict[str, Any]:
        started = time.monotonic()
        cancelled = threading.Event()
        cancellation = None
        activity_id = 'inspection-' + uuid.uuid4().hex
        metrics = {'scans': [], 'scan_seconds': 0., 'directories': 0, 'entries': 0,
                   'prepare_seconds': 0., 'command_seconds': 0., 'spawn_seconds': 0.}
        result = {'tool': 'exec_command', 'observation_only': True,
                  'hostname': socket.gethostname(), 'stage': stage,
                  'observed_at': datetime.now(timezone.utc).isoformat(),
                  'environment_scope': 'sanitized_read_only_sandbox',
                  'exit_code': None, 'stdout': '', 'stderr': '', 'timed_out': False,
                  'timing': metrics, 'activity_id': activity_id,
                  'scope_note': 'Only cwd immediate entries, typed files and read_paths are mounted. An unmounted subtree is not evidence of absence.'}
        limit = 12000
        phase = 'preparing'
        phase_start = started

        def display(event, text):
            from agent_core.cli_display import emit
            emit('activity', f'{stage or "本地"} · {text} · {time.monotonic()-phase_start:.1f}s',
                 purpose='status', task_id=task_id, node=stage, call_id=activity_id,
                 event=event, progress_phase=event, source='runtime_fact')

        async def wait_worker(worker):
            # Timed observation also works when outer sandboxes suppress the
            # cross-thread event-loop wakeup. It does not change task deadlines.
            while not worker.done():
                await asyncio.wait({worker}, timeout=.02)
            return worker.result()

        async def heartbeat():
            while True:
                await asyncio.sleep(1)
                display(phase, '准备检查环境' if phase == 'preparing' else '执行本地检查')

        display('preparing', '准备检查环境')
        ticker = asyncio.create_task(heartbeat())
        try:
            if not isinstance(arguments, Mapping) or set(arguments) - {'cmd', 'workdir', 'timeout_ms', 'max_output_chars', 'read_paths'}:
                raise CommandInspectionError('Invalid exec_command arguments')
            cmd = arguments.get('cmd')
            if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 32768 or '\x00' in cmd:
                raise CommandInspectionError('cmd must be a nonempty shell script of at most 32768 characters')
            timeout_ms = arguments.get('timeout_ms', 30000)
            limit = arguments.get('max_output_chars', 12000)
            paths = arguments.get('read_paths', [])
            if not isinstance(paths, list) or len(paths) > 64 or any(not isinstance(p, str) or not p.strip() or '\x00' in p for p in paths):
                raise CommandInspectionError('read_paths must contain at most 64 explicit paths')
            if type(timeout_ms) is not int or not 100 <= timeout_ms <= 120000:
                raise CommandInspectionError('timeout_ms must be an integer in [100, 120000]')
            if type(limit) is not int or not 256 <= limit <= 64000:
                raise CommandInspectionError('max_output_chars must be an integer in [256, 64000]')
            deadline = started + timeout_ms / 1000
            def check():
                if cancelled.is_set():
                    raise CommandInspectionError('COMMAND_PREPARATION_CANCELLED')
                if time.monotonic() >= deadline:
                    raise TimeoutError('COMMAND_PREPARATION_TIMEOUT')
            self._refresh_runtime()
            project = self.project_for_task(task_id)
            workdir = arguments.get('workdir', '.')
            if not isinstance(workdir, str):
                raise CommandInspectionError('workdir must be a string')
            candidate = Path(workdir).expanduser()
            cwd = (candidate if candidate.is_absolute() else project / candidate).resolve(strict=True)
            if not cwd.is_dir():
                raise CommandInspectionError('workdir is not a directory')
            result.update({'cmd': cmd, 'cwd': str(cwd), 'project_dir': str(project)})
            with tempfile.TemporaryFile() as seccomp, tempfile.TemporaryFile() as registry:
                seccomp.write(_network_filter()); seccomp.seek(0)
                registry.write(('\n'.join(sorted(str(p) for p in self.prefixes if (p / 'conda-meta').is_dir())) + '\n').encode())
                registry.seek(0)
                worker = asyncio.create_task(asyncio.to_thread(self._argv, project=project, cwd=cwd, cmd=cmd,
                    filter_fd=seccomp.fileno(), registry_fd=registry.fileno(), read_paths=paths,
                    task_paths=[*self.path_context(task_id)['paths'],
                        *({'path': value} for record in self.recent_observations(task_id)['observations']
                          for value in (record.get('log_refs') or {}).values())], metrics=metrics, check=check))
                try:
                    argv = await wait_worker(worker)
                except asyncio.CancelledError as exc:
                    cancelled.set()
                    try:
                        await wait_worker(worker)
                    except (OSError, ValueError):
                        pass
                    cancellation = exc
                    raise
                finally:
                    metrics['prepare_seconds'] = time.monotonic() - started
                    metrics['prepared_at'] = datetime.now(timezone.utc).isoformat()
                check()
                phase, phase_start = 'executing', time.monotonic()
                display(phase, '执行本地检查')
                completion = asyncio.create_task(asyncio.to_thread(self._execute, argv,
                    (seccomp.fileno(), registry.fileno()), max(0, deadline-time.monotonic()), cancelled, metrics))
                try:
                    returncode, retained, sizes, timed_out = await wait_worker(completion)
                except asyncio.CancelledError as exc:
                    cancelled.set()
                    returncode, retained, sizes, timed_out = await wait_worker(completion)
                    cancellation = exc
                metrics['command_seconds'] = time.monotonic() - phase_start
                metrics['command_ended_at'] = datetime.now(timezone.utc).isoformat()
            result['exit_code'] = returncode
            result['timed_out'] = timed_out
            result['status'] = 'cancelled' if cancellation else 'timed_out' if timed_out else 'completed'
            output = {name: bytes(data).decode('utf-8', errors='replace') for name, data in retained.items()}
            if returncode and output['stderr'].startswith('bwrap:'):
                result.update(status='sandbox_error', error='COMMAND_SANDBOX_FAILED; observations do not establish file/tool absence')
            result['stream_bytes'] = sizes
            result['truncated'] = {n: len(v) > limit or sizes[n] > len(retained[n]) for n, v in output.items()}
            result['log_truncated'] = {n: sizes[n] > len(retained[n]) for n in sizes}
        except asyncio.CancelledError as exc:
            cancellation = exc
            result['status'] = 'cancelled'
            output = {'stdout': '', 'stderr': ''}
        except (OSError, ValueError, TypeError) as exc:
            result.update(status='timed_out' if isinstance(exc, TimeoutError) else 'not_run',
                          error=str(exc), timed_out=isinstance(exc, TimeoutError))
            output = {'stdout': '', 'stderr': ''}
        finally:
            ticker.cancel()
        observation = self._task_dir(task_id) / activity_id.removeprefix('inspection-')
        observation.mkdir(parents=True, exist_ok=False)
        result['log_refs'] = {}
        for name, content in output.items():
            path = observation / f'{name}.log'
            path.write_text(content, encoding='utf-8')
            result['log_refs'][name] = str(path)
            result[name] = content[:limit]
        result['duration_seconds'] = time.monotonic() - started
        metrics['returned_at'] = datetime.now(timezone.utc).isoformat()
        result['receipt_ref'] = str(observation / 'receipt.json')
        self._record_metrics(task_id, result)
        (observation / 'receipt.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        from agent_core.cli_display import emit
        emit('details', json.dumps(result, ensure_ascii=False), task_id=task_id, node=stage,
             call_id=activity_id, purpose='telemetry')
        phase_start = started
        display(result['status'], '本地检查' + {'completed': '结束', 'cancelled': '已取消', 'timed_out': '超时'}.get(result['status'], '未执行'))
        if cancellation:
            raise cancellation
        return result
