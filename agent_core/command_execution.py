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


def _private_paths(roots, *, recursive=True):
    """Inspect names only. Never open credential files or follow directory links."""
    for root in roots:
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in list(dirs) + files:
                if _PRIVATE_PATTERN.fullmatch(name.lower()):
                    path = Path(directory) / name
                    yield path
                    if name in dirs:
                        dirs.remove(name)
            if not recursive:
                break


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
            resolved = str(Path(item).resolve())
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
        active = os.environ.get('CONDA_PREFIX', '')
        self.active_conda_prefix = str(Path(active).resolve()) if active and Path(active).is_dir() else ''

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
        if not binding.is_file() or self.context.explicit:
            directory.mkdir(parents=True, exist_ok=True)
            value = {'project_dir': str(project), 'startup_dir': str(self.context.startup_dir),
                     'source': self.context.source, 'hostname': socket.gethostname()}
            temporary = binding.with_name(f'.context-{uuid.uuid4().hex}.tmp')
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(binding)
        return project

    def describe(self, task_id='') -> dict[str, Any]:
        return {'project_dir': str(self.project_for_task(task_id)),
                'startup_dir': str(self.context.startup_dir), 'hostname': socket.gethostname(),
                'read_roots': [str(p) for p in (self.data_path, self.install_dir, *self.extra_roots)],
                'conda_prefixes': sorted(str(p) for p in self.prefixes if (p / 'conda-meta').is_dir()),
                'host_conda_prefix': self.active_conda_prefix,
                'python_executable': sys.executable,
                'sandbox': 'read_only; private_tmp; network_disabled',
                'command_log_dir': str(self._task_dir(task_id)),
                'bwrap_installed': bool(self.bwrap)}

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
                if value.get('hostname') != socket.gethostname() or value.get('tool') != 'exec_command':
                    continue
                fields = ('stage', 'status', 'cmd', 'cwd', 'project_dir', 'hostname',
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

    def _argv(self, *, project, cwd, cmd, filter_fd, registry_fd):
        if not self.bwrap or not Path(self.bwrap).is_file():
            raise CommandInspectionError('COMMAND_SANDBOX_UNAVAILABLE: install bubblewrap on this Linux host')
        if sys.platform != 'linux':
            raise CommandInspectionError('COMMAND_SANDBOX_UNSUPPORTED_OS: use a Linux host/container')
        project_roots = {project, self.data_path, self.install_dir, *self.extra_roots}
        project_roots = {p for p in project_roots if not any(p != q and p.is_relative_to(q) for q in project_roots)}
        roots = set(project_roots)
        roots.update(p for p in self.prefixes if p not in {Path('/usr'), Path('/usr/local')})
        roots.update(Path(p).resolve() for p in self.path if not p.startswith(('/usr/', '/bin', '/sbin')))
        roots = {_directory(p) for p in roots}
        roots = sorted(p for p in roots if not any(p != q and p.is_relative_to(q) for q in roots))
        system_roots = [Path(p) for p in ('/usr', '/bin', '/sbin', '/lib', '/lib64') if Path(p).exists()]
        if not any(cwd == root or cwd.is_relative_to(root) for root in (*roots, *system_roots)):
            raise CommandInspectionError(f'COMMAND_WORKDIR_OUTSIDE_READ_ROOTS: {cwd}')
        argv = [self.bwrap, '--unshare-user', '--unshare-ipc', '--unshare-pid', '--unshare-uts',
                '--die-with-parent', '--cap-drop', 'ALL', '--seccomp', str(filter_fd),
                '--tmpfs', '/tmp', '--dir', '/tmp/biomind-home', '--dir', '/tmp/biomind-home/.conda']
        # /tmp precedes project mounts, including projects that themselves live in /tmp.
        for root in system_roots:
            argv += ['--ro-bind', str(root.resolve()), str(root)]
        for path in ('/etc/ld.so.cache', '/etc/localtime'):
            if Path(path).is_file():
                argv += ['--ro-bind', path, path]
        # Runtime prefixes are installed software. Mask their root configuration
        # files without recursively walking millions of public package files on
        # every query; project/data roots still receive recursive name filtering.
        private = set(_private_paths(project_roots))
        private.update(_private_paths(self.prefixes | set(roots), recursive=False))
        hidden = private | set(self.deny_paths)
        hidden |= {p.resolve() for p in hidden if p.is_symlink()}
        for root in roots:
            argv += _readonly_mounts(root, hidden)
        argv += ['--proc', '/proc', '--dev', '/dev', '--ro-bind-data', str(registry_fd),
                 '/tmp/biomind-home/.conda/environments.txt', '--chdir', str(cwd)]
        env = {'PATH': ':'.join(self.path), 'HOME': '/tmp/biomind-home', 'TMPDIR': '/tmp',
               'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONNOUSERSITE': '1', 'CONDARC': '/dev/null', 'CONDA_NO_PLUGINS': 'true',
               'BIOMIND_PROJECT_DIR': str(project)}
        if self.conda:
            env['CONDA_EXE'] = self.conda
        if self.active_conda_prefix:
            env['CONDA_PREFIX'] = self.active_conda_prefix
        for key, value in sorted(env.items()):
            argv += ['--setenv', key, value]
        return argv + ['--', '/bin/bash', '--noprofile', '--norc', '-c', cmd]

    @staticmethod
    def _execute(argv, fds, timeout_seconds, cancelled):
        """Drain both pipes without unlimited buffering, including startup/exit.

        The worker owns Popen and its cleanup even if the async caller cancels.
        start_new_session gives the process tree a group separate from BioMind.
        """
        retained = {'stdout': bytearray(), 'stderr': bytearray()}
        sizes = {'stdout': 0, 'stderr': 0}
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        with subprocess.Popen(argv, env={}, cwd='/', stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              start_new_session=True, close_fds=True, pass_fds=fds) as process, _tracked_process(process):
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

    async def run(self, arguments: Mapping[str, Any], *, task_id='', stage='') -> dict[str, Any]:
        started = time.monotonic()
        cancellation = None
        result: dict[str, Any] = {'tool': 'exec_command', 'observation_only': True,
                                 'hostname': socket.gethostname(), 'stage': stage,
                                 'observed_at': datetime.now(timezone.utc).isoformat(),
                                 'environment_scope': 'sanitized_read_only_sandbox',
                                 'exit_code': None, 'stdout': '', 'stderr': '', 'timed_out': False}
        try:
            if not isinstance(arguments, Mapping) or set(arguments) - {'cmd', 'workdir', 'timeout_ms', 'max_output_chars'}:
                raise CommandInspectionError('Invalid exec_command arguments')
            cmd = arguments.get('cmd')
            if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 32768 or '\x00' in cmd:
                raise CommandInspectionError('cmd must be a nonempty shell script of at most 32768 characters')
            timeout_ms = arguments.get('timeout_ms', 30000)
            limit = arguments.get('max_output_chars', 12000)
            if type(timeout_ms) is not int or not 100 <= timeout_ms <= 120000:
                raise CommandInspectionError('timeout_ms must be an integer in [100, 120000]')
            if type(limit) is not int or not 256 <= limit <= 64000:
                raise CommandInspectionError('max_output_chars must be an integer in [256, 64000]')
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
                argv = await asyncio.to_thread(self._argv, project=project, cwd=cwd, cmd=cmd,
                                               filter_fd=seccomp.fileno(), registry_fd=registry.fileno())
                cancelled = threading.Event()
                completion = asyncio.create_task(asyncio.to_thread(self._execute, argv,
                    (seccomp.fileno(), registry.fileno()), timeout_ms / 1000, cancelled))
                try:
                    returncode, retained, sizes, timed_out = await asyncio.shield(completion)
                except asyncio.CancelledError as exc:
                    cancelled.set()
                    returncode, retained, sizes, timed_out = await asyncio.shield(completion)
                    cancellation = exc
            result['exit_code'] = returncode
            result['timed_out'] = timed_out
            output = {name: bytes(data).decode('utf-8', errors='replace') for name, data in retained.items()}
            result['status'] = 'cancelled' if cancellation is not None else 'timed_out' if result['timed_out'] else 'completed'
            if returncode and output['stderr'].startswith('bwrap:'):
                result['status'] = 'sandbox_error'
                result['error'] = 'COMMAND_SANDBOX_FAILED; command observations do not establish file/tool absence'
            observation = self._task_dir(task_id) / uuid.uuid4().hex
            observation.mkdir(parents=True, exist_ok=False)
            result['log_refs'] = {}
            result['truncated'] = {}
            for name, content in output.items():
                path = observation / f'{name}.log'
                path.write_text(content, encoding='utf-8')
                result['log_refs'][name] = str(path)
                result['truncated'][name] = len(content) > limit or sizes[name] > len(retained[name])
                result[name] = content[:limit]
            result['stream_bytes'] = sizes
            result['log_truncated'] = {name: sizes[name] > len(retained[name]) for name in sizes}
            result['duration_seconds'] = round(time.monotonic() - started, 3)
            result['receipt_ref'] = str(observation / 'receipt.json')
            (observation / 'receipt.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            if cancellation is not None:
                raise cancellation
        except (OSError, ValueError, TypeError) as exc:
            result.update(status='not_run', error=str(exc), duration_seconds=round(time.monotonic() - started, 3))
        return result
