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
from contextlib import contextmanager, ExitStack
from pathlib import Path
from typing import Any, Mapping

from agent_core.process_registry import ACTIVE_PROCESS_GROUPS
from agent_core.command_inspection_owner import InspectionOwnerMixin
from agent_core.project_directory import ProjectDirectoryError, validate_project_directory


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


def _signal_owned_group(process):
    from execution.executor import _process_identity
    expected = getattr(process, '_biomind_creation_identity', {})
    current = _process_identity(process.pid)
    if (expected.get('process_start_ticks') is None or current != expected):
        return False
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


@contextmanager
def _tracked_process(process):
    from execution.executor import _process_identity
    process._biomind_creation_identity = _process_identity(process.pid)
    ACTIVE_PROCESS_GROUPS.add(process.pid)
    try:
        yield
    finally:
        try:
            if process.poll() is None:
                _signal_owned_group(process)
                process.wait()
        finally:
            ACTIVE_PROCESS_GROUPS.discard(process.pid)


def _directory(value: str | Path) -> Path:
    try:
        return validate_project_directory(value)
    except ProjectDirectoryError as error:
        failure = CommandInspectionError(str(error))
        failure.code = error.code
        raise failure from error


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


def _readonly_mounts(root: Path, hidden: set[Path], *, bind=None) -> list[str]:
    """Mirror only directories containing excluded entries; never open a secret.

    Overlaying a secret *after* binding its parent can fail under a surrounding
    filesystem policy. Excluding it before binding also avoids that transient
    exposure and preserves ordinary symlinks without granting their targets.
    """
    bind = bind or (lambda path: ['--ro-bind', str(path), str(path)])
    excluded = {p for p in hidden if p == root or p.is_relative_to(root)}
    if not excluded:
        return bind(root)
    argv = ['--tmpfs', str(root)]
    if root not in excluded:
        for child in sorted(root.iterdir()):
            if child in excluded:
                continue
            if child.is_symlink():
                argv += ['--symlink', os.readlink(child), str(child)]
            elif child.is_dir() and any(p.is_relative_to(child) for p in excluded):
                argv += _readonly_mounts(child, excluded, bind=bind)
            else:
                argv += bind(child)
    argv += ['--remount-ro', str(root)]
    return argv


class FormalProtectionError(ValueError):
    """Protected execution was not launched; never retry as a host command."""
    def __init__(self, reason, **details):
        self.code = 'FORMAL_READ_PROTECTION_UNAVAILABLE'
        self.diagnostic = {'status': 'capability_missing', 'reason': reason,
                           'phase': 'access_projection', **details}
        super().__init__(reason)


_FORMAL_CAPABILITY_CACHE = {}


def formal_protection_capability(projection):
    """One bounded namespace probe per installed bwrap identity, not per file."""
    binary = str(projection.get('bwrap_path') or '')
    if sys.platform != 'linux' or not binary or not Path(binary).is_file():
        raise FormalProtectionError('bubblewrap_missing_or_platform_unsupported',
                                    phase='backend_probe', backend='bubblewrap')
    try: info = Path(binary).stat()
    except OSError as exc:
        raise FormalProtectionError('bubblewrap_probe_failed', phase='backend_probe',
            backend='bubblewrap', errno=exc.errno) from exc
    key = (binary, info.st_dev, info.st_ino, info.st_mtime_ns)
    if key not in _FORMAL_CAPABILITY_CACHE:
        argv = [binary, '--unshare-user', '--unshare-pid', '--die-with-parent',
                '--cap-drop', 'ALL']
        for name in ('/usr', '/bin', '/lib', '/lib64'):
            if Path(name).exists(): argv += ['--ro-bind', str(Path(name).resolve()), name]
        try:
            result = subprocess.run(argv + ['--proc', '/proc', '--dev', '/dev',
                '--', '/bin/true'], stdin=subprocess.DEVNULL, capture_output=True,
                text=True, timeout=5, close_fds=True)
            _FORMAL_CAPABILITY_CACHE[key] = (result.returncode, result.stderr[-2000:], None)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _FORMAL_CAPABILITY_CACHE[key] = (None, type(exc).__name__ + ': ' + str(exc), getattr(exc,'errno',None))
    code, diagnostic, error_number = _FORMAL_CAPABILITY_CACHE[key]
    if code != 0:
        raise FormalProtectionError('bubblewrap_namespace_unavailable',
                                    phase='backend_probe', backend='bubblewrap',
                                    probe_exit_code=code, probe_stderr=diagnostic,
                                    **({'errno':error_number} if error_number is not None else {}))
    return {'status': 'available', 'bwrap_path': binary, 'probe_exit_code': code}


def protected_path_roots(service, task_id, arguments=None):
    """One host-derived classification shared by file I/O and command mounts.

    Reuses registered runtime resolution; it grants no data access. Host draft
    and ledger maintenance retain their own explicitly selected writers.
    """
    arguments = arguments or {}
    project = Path(service.project_for_task(task_id)).resolve(strict=True)
    data = Path(service.data_path).resolve(strict=True)
    if hasattr(service, '_refresh_runtime'): service._refresh_runtime()
    runtime = {Path(p).resolve() for p in getattr(service, 'prefixes', ())}
    runtime.update(Path(p).resolve() for p in getattr(service, 'path', ()) if Path(p).is_dir())
    # Registry selection supplies launch facts, never directory authority.
    # Only host-configured runtime roots, the project, or an existing live
    # directory grant can supply a selected prefix to protected execution.
    runtime_grants=[]
    for key in ('ENV_PREFIX', 'CONDA_PREFIX'):
        prefix=(arguments.get('env') or {}).get(key)
        if not prefix:continue
        candidate=Path(prefix).resolve(strict=True)
        known=candidate.is_relative_to(project) or any(candidate==p or candidate.is_relative_to(p) for p in runtime)
        if not known:
            from agent_core.execution_permissions import active_read_grants, path_grants, digest
            path_ids={g['grant_id'] for g in path_grants(service,task_id,candidate,'read')}
            grants=active_read_grants(service.data_path,task_id,project=project)
            matches=[g for g in grants if g.get('target_kind')=='directory' and g.get('recursive') is True
                     and (candidate==Path(g['root']) or candidate.is_relative_to(Path(g['root'])))
                     and (g.get('operation')=='file_read' or g['grant_id'] in path_ids)]
            if not matches:
                raise FormalProtectionError('runtime_prefix_permission_required',path=str(candidate),
                    instruction='Registered location is not directory permission; use the existing path-access approval for this exact runtime prefix.')
            grant=max(matches,key=lambda g:len(Path(g['root']).parts))
            ref={'grant_id':grant['grant_id'],'grant_hash':digest(grant),'prefix':str(candidate)}
            if ref not in runtime_grants:runtime_grants.append(ref)
        runtime.add(candidate)
    runtime.add(Path(getattr(service, 'install_dir', Path(__file__).resolve().parents[1])).resolve())
    runtime = {p for p in runtime if p.is_dir() and p not in {Path('/'), Path('/home'), Path('/public'), Path('/public/home'), Path('/tmp'), Path.home().resolve()}}
    runtime = {p for p in runtime if not any(p != q and p.is_relative_to(q) for q in runtime)}
    controls = {data, project / '.biomind', project / '.git'}
    return {'project': project, 'data': data, 'runtime': runtime, 'runtime_grants':runtime_grants, 'controls': controls,
            'denies': {Path(p).resolve() for p in getattr(service, 'deny_paths', ())}}


def check_data_path(service, task_id, value, *, write=False):
    """Classify lexical and canonical targets before any model file operation."""
    facts = protected_path_roots(service, task_id)
    try:targets={Path(value).absolute(),Path(value).resolve()}
    except RuntimeError as exc:raise ValueError('FILE_SYMLINK_LOOP') from exc
    for target in targets:
        if any(_PRIVATE_PATTERN.fullmatch(part.lower()) or part.lower() in
               {'auth.json', 'privatekeys', '.biomind', '.git'} for part in target.parts):
            raise ValueError('PRIVATE_FILE_EXCLUDED')
        if any(target == p or target.is_relative_to(p) for p in facts['controls'] | facts['denies']):
            raise ValueError('PROTECTED_CONTROL_PATH')
        if write and any(target == p or target.is_relative_to(p) for p in facts['runtime'] |
                         {Path(p).resolve() for p in ('/usr','/bin','/sbin','/lib','/lib64','/etc')}):
            raise ValueError('PROTECTED_RUNTIME_PATH: ordinary data approval cannot permit runtime writes')
    return facts


def snapshot_read_directory(service, task_id, root):
    """Bounded metadata-only view; exact RO mounts, never a recursive data bind."""
    from agent_core.execution_permissions import _safe_read_info
    root=Path(root);files=[];directories=[];pending=[root];started=time.monotonic();entries=0;excluded=0
    facts=protected_path_roots(service,task_id)
    while pending:
        current=pending.pop();info=_safe_read_info(current)
        directories.append({'root':str(current),'anchor':[info.st_dev,info.st_ino]})
        with os.scandir(current) as iterator:
            for entry in iterator:
                entries+=1
                if entries>256 or time.monotonic()-started>2.0:
                    raise FormalProtectionError('directory_snapshot_limit',path=str(root),phase='directory_projection',
                        entries=entries,entry_limit=256,time_limit_seconds=2.0,mount_limit=512,
                        instruction='目录已批准，但当前后端不能安全提供这种大小的目录视图。请声明更小子目录；未截断执行，不需要重新授权同一范围。')
                path=Path(entry.path)
                if (_PRIVATE_PATTERN.fullmatch(entry.name.lower()) or entry.name.lower() in {'auth.json','privatekeys','.biomind','.git'}
                        or any(path==p or path.is_relative_to(p) for p in facts['controls']|facts['denies'])):
                    excluded+=1;continue
                if entry.is_symlink():
                    raise FormalProtectionError('directory_snapshot_symlink',path=str(path),phase='directory_projection',
                        instruction='目录快照不跟随链接；请明确声明所需链接文件，由原 canonical 文件授权路径核验。')
                item=_safe_read_info(path)
                if stat.S_ISDIR(item.st_mode):pending.append(path)
                else:files.append(path)
    return sorted(files),sorted(directories,key=lambda r:r['root']),{'root':str(root),'entries':entries,'files':len(files),'directories':len(directories),
        'excluded_private_entries':excluded,'time_limit_seconds':2.0,'entry_limit':256,
        'mount_limit':512,'visibility':'metadata snapshot; later additions are not visible'}


def formal_access_projection(service, arguments, task_id, *, read_access):
    """Trusted mount description included in the EXISTING command approval.

    Resources are roots, never expanded sample lists. This does not grant any
    resource: read_access is assembled by execution_permissions from live grants.
    """
    from copy import deepcopy
    project = Path(service.project_for_task(task_id)).resolve(strict=True)
    data = Path(service.data_path).resolve(strict=True)
    for read in read_access.get('reads', []):
        if read.get('recursive') is True or read.get('target_kind', read.get('anchor_type')) == 'directory':
            raise FormalProtectionError('recursive_read_privacy_filter_unavailable',
                path=read['root'], affected_capability='recursive_directory_formal_execution',
                file_tools_read_grant_retained=True,
                instruction='目录读取授权仍有效，但正式命令不支持递归目录挂载。请通过 read_paths 声明实际需要的精确文件；不要扩大目录或重复请求授权。')
    cwd = (project / Path(arguments.get('workdir') or project)).resolve(strict=True)
    if not cwd.is_relative_to(project) and not any(cwd == Path(r['root']) for r in read_access.get('read_directories', [])) and not any(
            cwd == Path(r['root']) or cwd.is_relative_to(Path(r['root'])) for r in read_access.get('write_targets', [])):
        raise FormalProtectionError('cwd_directory_permission_required', path=str(cwd), requested_workdir=str(cwd))
    facts = protected_path_roots(service, task_id, arguments)
    runtime, controls = facts['runtime'], facts['controls']
    probe = arguments.get('_biomind_probe') is True
    from agent_core.plan_tracker import load_plan_state
    from agent_core.execution_permissions import _lifetime_current, path_grants, scoped_automatic_grants
    live_state=load_plan_state(service.data_path,task_id)
    automatic=live_state.get('task_state',{}).get('execution_permissions',{}).get('automation',{})
    isolate_network=probe or (automatic.get('mode')=='auto' and _lifetime_current(live_state,automatic)) or bool(scoped_automatic_grants(live_state))
    writable = set() if probe else {project}
    if probe and (arguments.get('write_paths') or arguments.get('delete_paths')):
        raise FormalProtectionError('probe_cannot_declare_writes')
    # Explicit write_paths retain their existing reviewed output purpose. Do
    # not create new directories before approval or widen missing paths to /.
    for raw in [*(arguments.get('write_paths') or []), *(arguments.get('delete_paths') or [])]:
        target = (cwd / Path(raw)).resolve()
        if any(_PRIVATE_PATTERN.fullmatch(part.lower()) for part in target.parts):
            raise FormalProtectionError('private_output_path', path=str(target))
        if any(target == p or target.is_relative_to(p) for p in controls):
            raise FormalProtectionError('control_state_not_writable', path=str(target))
        if target.is_relative_to(project): continue
        check_data_path(service,task_id,target,write=True)
        if not path_grants(service,task_id,target,'read_write'):
            raise FormalProtectionError('external_write_permission_required',path=str(target))
        writable.add(target)
    denies = {Path(p).absolute() for p in getattr(service, 'deny_paths', ())}
    # Controls are host-owned even when the project itself is a result run.
    for root in writable:
        if root in {Path('/'), Path('/home'), Path('/public'), Path('/public/home'), Path('/tmp'), Path.home().resolve()}:
            raise FormalProtectionError('writable_root_too_broad', path=str(root))
        if any(root == p or root.is_relative_to(p) for p in controls):
            raise FormalProtectionError('control_state_not_writable', path=str(root))
        if not root.is_relative_to(project) and (any(root == p or root.is_relative_to(p) for p in runtime) or any(
                root == p or root.is_relative_to(p) or p.is_relative_to(root)
                for p in {Path('/usr'), Path('/bin'), Path('/sbin'), Path('/lib'), Path('/lib64'), Path('/etc')})):
            raise FormalProtectionError('writable_system_runtime_overlap', path=str(root))
        for read in [*read_access.get('reads', []),*read_access.get('directory_snapshots', [])]:
            source = Path(read['root'])
            if root == project: continue  # Explicit inputs are overlaid RO after the implicit project bind.
            if source == root or source.is_relative_to(root) or root.is_relative_to(source):
                raise FormalProtectionError('input_output_overlap', input=str(source), output=str(root))
    writable = {p for p in writable if not any(p != q and p.is_relative_to(q) for q in writable)}
    mount_anchors = {}
    for root in runtime | {p for p in writable if p.exists()} | {data, project}:
        try: info = root.stat()
        except OSError as exc:
            raise FormalProtectionError('mount_source_stat_failed', phase='mount_anchor_check',
                backend='bubblewrap', path=str(root), errno=exc.errno) from exc
        mount_anchors[str(root)] = [info.st_dev, info.st_ino]
    return {**deepcopy(read_access), 'mount_policy': 'consult-probe-ro-v1' if probe else 'formal-input-ro-v1',
            'probe':probe, 'limits':{'wall_seconds':15,'output_bytes':65536,'cpu_seconds':10,
                'address_space_bytes':2147483648,'file_bytes':8388608,'processes':64} if probe else None,
            'network_policy':'isolated' if isolate_network else 'manual-command-policy',
            'project': str(project), 'data_path': str(data),
            'task_read_root': str(data / 'task_outputs' / str(task_id)), 'cwd': str(cwd),
            'runtime_roots': sorted(map(str, runtime)), 'runtime_grants':facts['runtime_grants'],
            'runtime_grant_task_id':str(task_id), 'write_roots': sorted(map(str, writable)),
            'control_roots': sorted(map(str, controls)), 'deny_paths': sorted(map(str, denies)),
            'mount_anchors': mount_anchors,
            'bwrap_path': str(getattr(service, 'bwrap', '') or shutil.which('bwrap', path='/usr/bin:/bin') or '')}


def _check_formal_mount_layout(writable_roots, protected_roots, readonly_overlays, masks):
    """Bounded static topology check for this launch, not a host stability proof.

    Reject surviving RW submounts. Root/ancestor binds are interpreted through
    mountinfo backing roots so a bind of protected data at the project root is
    not mistaken for an ordinary directory. No filesystem-wide inode search.
    """
    def fail(reason, **details):
        raise FormalProtectionError(reason, phase='mount_layout_check', backend='bubblewrap', **details)
    try:
        with open('/proc/self/mountinfo', encoding='utf-8') as stream:
            raw=stream.read(4*1024*1024+1)
    except OSError as exc:
        fail('mountinfo_read_failed', errno=exc.errno)
    if len(raw)>4*1024*1024: fail('mountinfo_limit_exceeded')
    def path_field(value):
        value=re.sub(r'\\(040|011|012|134)', lambda m: chr(int(m[1],8)), value)
        path=Path(value)
        if not path.is_absolute() or '..' in path.parts: raise ValueError('invalid mount path')
        return path
    records=[]
    try:
        for line in raw.splitlines():
            fields=line.split(); separator=fields.index('-')
            if len(fields)<10 or separator<6 or len(fields)<separator+4: raise ValueError('invalid mount row')
            if not re.fullmatch(r'\d+:\d+',fields[2]): raise ValueError('invalid device')
            records.append((int(fields[0]),fields[2],path_field(fields[3]),path_field(fields[4])))
    except (ValueError,IndexError): fail('mountinfo_parse_failed')
    def mounted(path):
        candidates=[r for r in records if path.is_relative_to(r[3])]
        if not candidates: fail('unsupported_writable_mount_layout',path=str(path))
        depth=max(len(r[3].parts) for r in candidates)
        matches=[r for r in candidates if len(r[3].parts)==depth]
        # Stacked mountpoints need visibility information this limited checker
        # does not have. Do not guess which backing root wins.
        if len(matches)!=1: fail('unsupported_writable_mount_layout',path=str(path))
        row=matches[0]
        return row[1], row[2]/path.relative_to(row[3])
    def excluded(path):
        return (any(path.is_relative_to(p) for p in readonly_overlays | masks)
                or any(_PRIVATE_PATTERN.fullmatch(part.lower()) for part in path.parts))
    for root in writable_roots:
        if any(p!=root and p.is_relative_to(root) and not excluded(p) for _,_,_,p in records):
            fail('unsupported_writable_mount_layout',path=str(root),detail='Unaccounted writable submount')
        device, backing=mounted(root)
        # Include nested protected mounts: their backing root can differ from
        # that of the protected parent. This list is finite mount topology.
        sources=set(protected_roots)
        sources.update(p for _,_,_,p in records if any(p.is_relative_to(r) for r in protected_roots))
        for source in sources:
            other_device, other_backing=mounted(source)
            if device!=other_device: continue
            if other_backing.is_relative_to(backing):
                alias=root/other_backing.relative_to(backing)
                original=source
            elif backing.is_relative_to(other_backing):
                alias=root
                original=source/backing.relative_to(other_backing)
            else: continue
            # Same lexical entry is either covered by the later RO overlay or
            # deliberately reopened (project inside runtime, runtime==project).
            if alias==original or excluded(alias): continue
            fail('unsupported_writable_mount_layout',path=str(alias),
                 detail='Writable backing range aliases a protected mount source')
    return {'mount_records_checked':len(records),'static_mount_layout':'checked'}


@contextmanager
def formal_protected_spawn(projection, command, *, cwd, env=None, observation=None):
    """Wrap the original executor spawn, retaining its logs and process owner.

    No network/env policy from inspection is copied. Only the process namespace
    and mounts change. Sources are open FD anchors until bwrap consumes them;
    unrelated and writable inherited FDs are closed by the original spawn.
    """
    formal_protection_capability(projection)
    from agent_core.execution_permissions import digest
    started = time.monotonic()
    metrics = {'rw_directories': 0, 'rw_entries': 0, 'ro_subtrees_pruned':0,
               'scan_complete':False, 'scan_roots':list(projection.get('write_roots', [])),
               'private_scan': 'declared_roots_top_level_only'}
    fds = []
    observed = {}
    created_anchors = {}
    def bind(path, *, writable=False, expected=None):
        path = Path(path)
        flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
        try: fd = os.open(path, flags)
        except OSError as exc:
            raise FormalProtectionError('mount_source_open_failed',path=str(path),
                phase='mount_anchor_check',backend='bubblewrap',errno=exc.errno) from exc
        fds.append(fd)
        try:
            info = os.fstat(fd)
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise FormalProtectionError('mount_source_stat_failed', phase='mount_anchor_check',
                backend='bubblewrap', path=str(path), errno=exc.errno) from exc
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise FormalProtectionError('unsupported_mount_source', phase='mount_anchor_check', path=str(path))
        identity = [info.st_dev, info.st_ino]
        expected = expected if expected is not None else created_anchors.get(str(path), projection.get('mount_anchors', {}).get(str(path)))
        if expected is not None and identity != list(expected):
            raise FormalProtectionError('read_anchor_changed_at_mount', phase='mount_anchor_check', path=str(path))
        if os.path.realpath('/proc/self/fd/' + str(fd)) != str(resolved):
            raise FormalProtectionError('mount_path_changed', phase='mount_anchor_check', path=str(path))
        observed[str(path)] = identity
        return ['--bind' if writable else '--ro-bind', '/proc/self/fd/' + str(fd), str(path)]
    try:
        if projection.get('runtime_grants'):
            from agent_core.execution_permissions import active_read_grants, digest, PermissionDenied
            live={g['grant_id']:g for g in active_read_grants(projection['data_path'],projection['runtime_grant_task_id'],project=projection['project'])}
            for ref in projection['runtime_grants']:
                if ref['grant_id'] not in live or digest(live[ref['grant_id']])!=ref['grant_hash']:
                    raise PermissionDenied('RUNTIME_DIRECTORY_GRANT_REVOKED_OR_CHANGED')
        # Only reached inside the claimed executor. Reuse the file adapter's
        # dirfd walk to create precisely the approved missing output, never its
        # ancestor as a writable mount. The original payload remains immutable.
        from agent_core.execution_permissions import _file_dispatch, _safe_read_info, _dispatch, record_created_directory
        from agent_core.workspace_files import WorkspaceFileService
        for target in projection.get('write_targets', []):
            path=Path(target['root'])
            if target.get('exists'):continue
            if path.exists():raise FormalProtectionError('write_target_appeared_before_spawn',phase='mount_anchor_check',path=str(path))
            anchor=Path(target['anchor_path']);info=_safe_read_info(anchor)
            if [info.st_dev,info.st_ino]!=target['anchor']:
                raise FormalProtectionError('write_anchor_changed',phase='mount_anchor_check',path=str(anchor))
            dispatch=_dispatch.get();permit=dispatch[0] if dispatch else None
            token=_file_dispatch.set((anchor,target['anchor'],permit))
            try:
                with WorkspaceFileService(None)._parent(anchor,path.relative_to(anchor),create=True) as (parent,name):
                    if target['target_kind']=='directory':os.mkdir(name,0o755,dir_fd=parent)
                    else:
                        fd=os.open(name,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o644,dir_fd=parent);os.close(fd)
                info=_safe_read_info(path);created_anchors[str(path)]=[info.st_dev,info.st_ino]
                if target['target_kind']=='directory' and permit:
                    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                    try:record_created_directory(path,fd,permit)
                    finally:os.close(fd)
            except OSError as exc:
                raise FormalProtectionError('output_creation_failed',phase='mount_anchor_check',path=str(path),errno=exc.errno) from exc
            finally:_file_dispatch.reset(token)
        masked_roots = set()
        argv = [projection['bwrap_path'], '--unshare-user', '--unshare-pid',
                '--die-with-parent', '--cap-drop', 'ALL', '--tmpfs', '/tmp']
        probe = projection.get('probe') is True
        if projection.get('network_policy')=='isolated':
            # Reuse the inspection executor's existing kernel socket/io_uring
            # filter. It also works where creating a network namespace's
            # loopback NETLINK socket is forbidden by the outer sandbox.
            with tempfile.TemporaryFile(prefix='biomind-network-filter-') as network_filter:
                network_filter.write(_network_filter()); network_filter.flush(); network_filter.seek(0)
                network_fd=os.dup(network_filter.fileno())
            fds.append(network_fd)
            argv += ['--seccomp',str(network_fd),'--unshare-ipc','--unshare-uts']
        for name in ('/usr', '/bin', '/sbin', '/lib', '/lib64'):
            if Path(name).exists():
                # System aliases may be lexical symlinks; their immutable
                # resolved targets are bound to the original spelling.
                source = Path(name).resolve()
                mount = bind(source); mount[-1] = name; argv += mount
        for name in ('/etc/ld.so.cache', '/etc/localtime', '/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf', '/etc/passwd', '/etc/group', '/etc/ssl/certs'):
            if Path(name).exists():
                source = Path(name).resolve(); mount = bind(source); mount[-1] = name; argv += mount
        hidden = {Path(p) for p in projection.get('deny_paths', [])}
        ro_roots = {Path(p) for p in projection.get('runtime_roots', [])}
        if probe: ro_roots.add(Path(projection['project']))
        task_read_root = Path(projection['task_read_root'])
        if not probe and task_read_root.is_dir(): ro_roots.add(task_read_root)
        reads = projection.get('reads', [])
        ro_roots.update(Path(row['root']) for row in reads)
        expected = {str(row['root']): row['anchor'] for row in reads}
        # Bind only known roots. Do not recurse through sample directories or
        # discover all environments. Deep secret-name filtering is not claimed.
        hidden.update(_private_paths((p for p in ro_roots if p.is_dir()), recursive=False))
        controls = {Path(p) for p in projection.get('control_roots', [])}
        if probe: hidden.update(controls)
        runtime_roots = {Path(p) for p in projection.get('runtime_roots', [])}
        writable_roots = {Path(p) for p in projection.get('write_roots', [])}
        runtime_before = {p for p in runtime_roots if not any(p.is_relative_to(w) for w in writable_roots)}
        runtime_after = runtime_roots - runtime_before - writable_roots
        metrics.update(_check_formal_mount_layout(writable_roots,
            ro_roots | controls | hidden | {Path(p).resolve() for p in ('/usr','/bin','/sbin','/lib','/lib64','/etc') if Path(p).exists()},
            runtime_after | {Path(r['root']) for r in reads} | {Path(r['root']) for r in projection.get('directory_snapshots',[])}, controls | hidden))
        # Keep the conservative deployed scan until the unresolved concurrent
        # host-writer contract is settled. The isolated Phase B candidate uses
        # the same runtime_after decision here; no model/config trust switch.
        scan_readonly_roots = set()
        scan_started=time.monotonic()
        def scan_failed(exc):
            raise FormalProtectionError('writable_scope_scan_failed',phase='writable_scope_scan',
                backend='bubblewrap',path=str(exc.filename or root),errno=exc.errno,
                scan_seconds=time.monotonic()-scan_started, **metrics) from exc
        def checked_stat(path):
            try: return path.lstat()
            except OSError as exc: scan_failed(exc)
        for root in sorted(runtime_before, key=lambda p: len(p.parts)):
            if not root.exists(): raise FormalProtectionError('mount_source_missing', path=str(root))
            argv += _readonly_mounts(root, hidden, bind=bind)
        for raw in projection.get('write_roots', []):
            root = Path(raw)
            aliases = []
            identities=set()
            root_info = checked_stat(root)
            if stat.S_ISREG(root_info.st_mode) and root_info.st_nlink > 1:
                aliases.append(root)
                identities.add((root_info.st_dev,root_info.st_ino))
            masks = set(hidden)
            if not (stat.S_ISREG(root_info.st_mode) or stat.S_ISDIR(root_info.st_mode)):
                raise FormalProtectionError('writable_scope_scan_failed', phase='writable_scope_scan',
                    backend='bubblewrap', path=str(root), detail='unsupported writable root type', scan_complete=False)
            walk = os.walk(root, followlinks=False, onerror=scan_failed) if stat.S_ISDIR(root_info.st_mode) else ()
            for directory, dirs, files in walk:
                metrics['rw_directories'] += 1
                metrics['rw_entries'] += len(dirs) + len(files)
                parent = Path(directory)
                for name in list(dirs) + files:
                    path = parent / name
                    if path in scan_readonly_roots:
                        if name in dirs: dirs.remove(name)
                        metrics['ro_subtrees_pruned']+=1
                        continue
                    if path in controls or path in hidden or _PRIVATE_PATTERN.fullmatch(name.lower()) or name.lower() in {'.biomind','.git','auth.json','privatekeys'}:
                        masks.add(path)
                        if name in dirs: dirs.remove(name)
                        continue
                    info = checked_stat(path)
                    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                        aliases.append(path)
                        identities.add((info.st_dev,info.st_ino))
            if aliases:
                # Refuse the finite RW tree, without enumerating RO input inodes.
                raise FormalProtectionError('hardlink_in_writable_scope', phase='writable_scope_scan',
                    backend='bubblewrap', path=str(aliases[0]), count=len(aliases),
                    unique_inode_count=len(identities), scan_complete=True,
                    scan_seconds=time.monotonic()-scan_started, **{k:v for k,v in metrics.items() if k!='scan_complete'})
            argv += bind(root, writable=True)
            masks |= controls
            masks = {p for p in masks if not any(p != q and p.is_relative_to(q) for q in masks)}
            for path in sorted(masks, key=lambda p: len(p.parts)):
                if not path.is_relative_to(root) or not path.exists(): continue
                if path.is_symlink():
                    raise FormalProtectionError('masked_writable_symlink', path=str(path))
                if path.is_dir():
                    argv += ['--tmpfs', str(path)]; masked_roots.add(path)
                else: argv += ['--ro-bind', '/dev/null', str(path)]
        metrics.update(scan_complete=True,scan_seconds=time.monotonic()-scan_started)
        # A bounded directory snapshot is a synthetic RO tree of exact files.
        # No recursive host bind means excluded entries and later additions are
        # absent, even when the underlying directory has a session RW grant.
        snapshot_roots={Path(r['root']) for r in projection.get('directory_snapshots',[])}
        snapshot_roots={p for p in snapshot_roots if not any(p!=q and p.is_relative_to(q) for q in snapshot_roots)}
        for row in projection.get('read_directories',[]):
            info=_safe_read_info(row['root'])
            if [info.st_dev,info.st_ino]!=row['anchor']:
                raise FormalProtectionError('read_directory_anchor_changed',phase='mount_anchor_check',path=row['root'])
        for root in sorted(snapshot_roots,key=lambda p:len(p.parts)):
            argv += ['--tmpfs',str(root)];masked_roots.add(root)
        for row in projection.get('read_directories',[]):
            if Path(row['root']) not in snapshot_roots:argv += ['--dir',row['root']]
        for root in sorted((ro_roots - runtime_roots) | runtime_after | {Path(r['root']) for r in reads}, key=lambda p: len(p.parts)):
            if not root.exists(): raise FormalProtectionError('mount_source_missing', path=str(root))
            if any(root == Path(raw) for raw in projection.get('write_roots', [])):
                continue
            anchor = expected.get(str(root))
            if anchor is not None:
                argv += bind(root, expected=anchor)
                for path in sorted(hidden, key=lambda p: len(p.parts)):
                    if not path.is_relative_to(root) or not path.exists(): continue
                    if path.is_symlink():
                        raise FormalProtectionError('masked_read_symlink', path=str(path))
                    if path.is_dir():
                        argv += ['--tmpfs', str(path)]; masked_roots.add(path)
                    else: argv += ['--ro-bind', '/dev/null', str(path)]
            else:
                argv += _readonly_mounts(root, hidden, bind=bind)
        for path in sorted(masked_roots, key=lambda p: -len(p.parts)):
            argv += ['--remount-ro', str(path)]
        argv += ['--proc', '/proc', '--dev', '/dev', '--remount-ro', '/', '--chdir', str(cwd)]
        program = list(command) if isinstance(command, list) else ['/bin/sh', '-c', command]
        if not Path('/usr/bin/python3').is_file():
            raise FormalProtectionError('system_python_fd_cleanup_unavailable')
        # This installed bwrap keeps passed mount FDs open after setup. The
        # small trusted exec adapter closes them before the scientific process
        # can run, without introducing another process owner or a service. It
        # starts with env={} and -I -S so loader/site hooks cannot run before
        # close; the exact approved environment is restored only for exec.
        process_env=dict(os.environ if env is None else env)
        if probe:process_env.update(HOME='/tmp',TMPDIR='/tmp',PYTHONDONTWRITEBYTECODE='1',PIP_NO_INDEX='1')
        environment_bytes = json.dumps(process_env).encode()
        # Conda's Python on some supported hosts has no os.memfd_create.
        # TemporaryFile is an unlinked/O_TMPFILE inode with the same anonymous
        # FD transport properties; it never becomes a persistent env file.
        with tempfile.TemporaryFile(prefix='biomind-execution-environment-') as environment_file:
            environment_file.write(environment_bytes)
            environment_file.flush(); environment_file.seek(0)
            environment_fd = os.dup(environment_file.fileno())
        fds.append(environment_fd)
        limits = ('import resource; resource.setrlimit(resource.RLIMIT_CPU,(10,10)); '
                  'resource.setrlimit(resource.RLIMIT_AS,(2147483648,2147483648)); '
                  'resource.setrlimit(resource.RLIMIT_FSIZE,(8388608,8388608)); '
                  'resource.setrlimit(resource.RLIMIT_NPROC,(64,64)); '
                  'resource.setrlimit(resource.RLIMIT_NOFILE,(64,64)); ') if probe else ''
        bootstrap = ('import os,sys,json; e=json.load(os.fdopen(int(sys.argv[2]))); '
                     'os.closerange(3, int(sys.argv[1])); '+limits+'a=sys.argv[3:]; os.execvpe(a[0],a,e)')
        argv += ['--', '/usr/bin/python3', '-I', '-S', '-c', bootstrap,
                 str(max(fds, default=2) + 1), str(environment_fd), *program]
        if observation is not None:
            observation['filesystem_protection'] = {'status': 'prepared', 'policy': projection['mount_policy'],
                'projection_sha256': digest(projection), 'mount_anchors': observed,
                'preparation_seconds': time.monotonic() - started, **metrics}
        yield argv, tuple(fds)
    finally:
        for fd in fds: os.close(fd)


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


class CommandExecutionService(InspectionOwnerMixin):
    """One service shared by all phases; every observation has a durable receipt."""

    def __init__(self, *, data_path, project_dir=None, startup_dir=None,
                 read_roots=(), deny_paths=(), bwrap_path=None,
                 first_yield_ms=20000, cleanup_timeout_seconds=5., runtime_locations=None):
        self._init_owners(first_yield_ms, cleanup_timeout_seconds)
        self.context = ProjectContext.discover(project_dir, startup_dir=startup_dir)
        Path(data_path).expanduser().mkdir(parents=True, exist_ok=True)
        self.data_path = _directory(data_path)
        self.install_dir = Path(__file__).resolve().parents[1]
        configured_roots = os.environ.get('BIOMIND_COMMAND_READ_ROOTS', '').split(os.pathsep)
        self.extra_roots = tuple(_directory(p) for p in (*read_roots, *configured_roots) if p)
        configured_denies = os.environ.get('BIOMIND_COMMAND_DENY_PATHS', '').split(os.pathsep)
        self.deny_paths = tuple(Path(p).expanduser().absolute() for p in (*deny_paths, *configured_denies) if p)
        self.bwrap = bwrap_path or shutil.which('bwrap', path='/usr/bin:/bin') or ''
        self._explicit_runtime_locations = runtime_locations is not None
        self.prefixes, self.conda, self.path = runtime_locations if self._explicit_runtime_locations else _runtime_locations()
        self._static_key = None
        self._static_argv = ()
        self._runtime_signature = None if self._explicit_runtime_locations else self._environment_signature()
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
        if self._explicit_runtime_locations:
            return
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
    def _execute(argv, fds, timeout_seconds, cancelled, metrics=None, log_paths=None):
        """Drain both pipes without unlimited buffering, including startup/exit.

        The worker owns Popen and its cleanup even if the async caller cancels.
        start_new_session gives the process tree a group separate from BioMind.
        """
        retained = {'stdout': bytearray(), 'stderr': bytearray()}
        sizes = {'stdout': 0, 'stderr': 0}
        tails = {'stdout': bytearray(), 'stderr': bytearray()}
        storage_errors = {}
        metrics = metrics if metrics is not None else {}
        deadline = metrics.get('deadline_monotonic',
                               time.monotonic() + timeout_seconds if timeout_seconds is not None else None)
        timed_out = False
        spawn_started = time.monotonic()
        with ExitStack() as files, subprocess.Popen(argv, env={}, cwd='/', stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              start_new_session=True, close_fds=True, pass_fds=fds) as process, _tracked_process(process):
            streams = {}
            for name, path in (log_paths or {}).items():
                try:
                    streams[name] = files.enter_context(open(path, 'xb', buffering=0))
                except OSError as exc:
                    storage_errors[name] = type(exc).__name__ + ': ' + str(exc)
            if metrics is not None:
                metrics['spawn_seconds'] = time.monotonic() - spawn_started
                metrics['process_started_at'] = datetime.now(timezone.utc).isoformat()
                metrics['process_alive'] = True
                metrics['phase'] = 'running'
            with selectors.DefaultSelector() as selector:
                for name, stream in (('stdout', process.stdout), ('stderr', process.stderr)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                killed = False
                exit_seen_at = None
                while selector.get_map() or process.poll() is None:
                    now = time.monotonic()
                    if process.poll() is not None:
                        metrics['process_alive'] = False
                        metrics['process_exit_code'] = process.returncode
                        metrics['phase'] = 'finalizing'
                        if exit_seen_at is None:
                            exit_seen_at = now
                        if selector.get_map() and now - exit_seen_at >= metrics.get('cleanup_timeout_seconds', 5.):
                            # The owner stays alive and drains. Only waiting is bounded;
                            # no detached task/FD release is implied by this observation.
                            metrics['cleanup_unconfirmed'] = True
                            metrics['cleanup_reason'] = 'inherited_pipes_not_closed'
                    if not killed and (cancelled.is_set() or (deadline is not None and time.monotonic() >= deadline)):
                        if process.poll() is not None:
                            # Preserve natural exit, including nonzero exit; an
                            # old/reaped PID cannot authorize a group signal.
                            killed = True
                        elif _signal_owned_group(process):
                            timed_out = not cancelled.is_set()
                            metrics['termination_signal_sent'] = True
                            killed = True
                        else:
                            metrics['cleanup_unconfirmed'] = True
                            metrics['cleanup_reason'] = 'process_creation_identity_unconfirmed'
                    for key, _events in selector.select(timeout=0.05):
                        chunk = os.read(key.fileobj.fileno(), 16384)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        name = key.data
                        sizes[name] += len(chunk)
                        metrics['last_output_monotonic'] = time.monotonic()
                        metrics['stream_bytes_live'] = dict(sizes)
                        if name in streams and name not in storage_errors:
                            try:
                                if streams[name].write(chunk) != len(chunk):
                                    raise OSError("incomplete raw stream write")
                            except OSError as exc:
                                storage_errors[name] = type(exc).__name__ + ': ' + str(exc)
                        remaining = max(0, 65536 - len(retained[name]))
                        retained[name].extend(chunk[:remaining])
                        tails[name].extend(chunk)
                        del tails[name][:-65536]
                process.wait()
                metrics['process_alive'] = False
                metrics['process_exit_code'] = process.returncode
                metrics['cleanup_unconfirmed'] = False
                metrics['phase'] = 'finalizing'
            for name, stream in streams.items():
                try:
                    os.fsync(stream.fileno())
                except OSError as exc:
                    storage_errors[name] = type(exc).__name__ + ': ' + str(exc)
        for name in retained:
            extra = min(len(tails[name]), max(0, sizes[name] - len(retained[name])))
            if extra:
                retained[name].extend(tails[name][-extra:])
        metrics['stream_storage_errors'] = storage_errors
        metrics['stream_storage_complete'] = {
            name: bool(log_paths and name in log_paths and name not in storage_errors)
            for name in sizes}
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

    async def _run_owned(self, owner) -> dict[str, Any]:
        arguments, task_id, stage = owner.arguments, owner.task_id, owner.stage
        started = owner.started
        cancelled = owner.cancelled
        cancellation = None
        activity_id = owner.run_ref
        metrics = owner.metrics
        result = {'tool': 'exec_command', 'observation_only': True,
                  'hostname': socket.gethostname(), 'stage': stage,
                  'observed_at': datetime.now(timezone.utc).isoformat(),
                  'environment_scope': 'sanitized_read_only_sandbox',
                  'exit_code': None, 'stdout': '', 'stderr': '', 'timed_out': False,
                  'timing': metrics, 'activity_id': activity_id,
                  'scope_note': 'Only cwd immediate entries, typed files and read_paths are mounted. An unmounted subtree is not evidence of absence.'}
        observation = owner.directory
        log_paths = {name: observation / f'{name}.log' for name in ('stdout', 'stderr')}
        limit = 12000
        retained = {}
        phase = 'preparing'
        phase_start = started

        def display(event, text):
            self._emit_owner(owner, event, text)

        async def wait_worker(worker):
            # Timed observation also works when outer sandboxes suppress the
            # cross-thread event-loop wakeup. It does not change task deadlines.
            while not worker.done():
                await asyncio.wait({worker}, timeout=.02)
            return worker.result()

        async def heartbeat():
            while True:
                await asyncio.sleep(1)
                current = metrics.get('phase', phase)
                display(current, '准备检查环境' if current == 'preparing' else '检查进程收尾' if current == 'finalizing' else '执行本地检查')

        display('preparing', '准备检查环境')
        ticker = asyncio.create_task(heartbeat())
        try:
            if not isinstance(arguments, Mapping) or set(arguments) - {'cmd', 'workdir', 'timeout_ms', 'max_output_chars', 'read_paths'}:
                raise CommandInspectionError('Invalid exec_command arguments')
            cmd = arguments.get('cmd')
            if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 32768 or '\x00' in cmd:
                raise CommandInspectionError('cmd must be a nonempty shell script of at most 32768 characters')
            timeout_ms = arguments.get('timeout_ms')
            limit = arguments.get('max_output_chars', 12000)
            paths = arguments.get('read_paths', [])
            if not isinstance(paths, list) or len(paths) > 64 or any(not isinstance(p, str) or not p.strip() or '\x00' in p for p in paths):
                raise CommandInspectionError('read_paths must contain at most 64 explicit paths')
            if timeout_ms is not None and (type(timeout_ms) is not int or timeout_ms <= 0):
                raise CommandInspectionError('timeout_ms must be a positive integer or null')
            if type(limit) is not int or not 256 <= limit <= 64000:
                raise CommandInspectionError('max_output_chars must be an integer in [256, 64000]')
            deadline = owner.deadline
            def check():
                if cancelled.is_set():
                    raise CommandInspectionError('COMMAND_PREPARATION_CANCELLED')
                if deadline is not None and time.monotonic() >= deadline:
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
                phase, phase_start = 'running', time.monotonic()
                metrics['phase'] = phase
                display(phase, '执行本地检查')
                completion = asyncio.create_task(asyncio.to_thread(self._execute, argv,
                    (seccomp.fileno(), registry.fileno()), max(0, deadline-time.monotonic()) if deadline is not None else None, cancelled, metrics, log_paths))
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
            result['status'] = ('stopped' if metrics.get('stop_requested') else 'cancelled') if cancelled.is_set() and metrics.get('termination_signal_sent') else 'timed_out' if timed_out else 'completed'
            output = {name: bytes(data).decode('utf-8', errors='replace') for name, data in retained.items()}
            if returncode and output['stderr'].startswith('bwrap:'):
                result.update(status='sandbox_error', error='COMMAND_SANDBOX_FAILED; observations do not establish file/tool absence')
            result['stream_bytes'] = sizes
            result['truncated'] = {n: sizes[n] > limit or sizes[n] > len(retained[n]) for n, v in output.items()}
            result['log_truncated'] = {n: not metrics['stream_storage_complete'].get(n, False) for n in sizes}
            result['log_integrity'] = {'complete': dict(metrics['stream_storage_complete']), 'errors': dict(metrics['stream_storage_errors'])}
        except asyncio.CancelledError as exc:
            cancellation = exc
            result['status'] = 'cancelled'
            output = {'stdout': '', 'stderr': ''}
        except (OSError, ValueError, TypeError) as exc:
            result.update(status='cancelled' if cancelled.is_set() else 'timed_out' if isinstance(exc, TimeoutError) else 'not_run',
                          error=str(exc), timed_out=isinstance(exc, TimeoutError))
            output = {'stdout': '', 'stderr': ''}
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        result['log_refs'] = {}
        result['output_projection'] = {}
        for name, content in output.items():
            path = log_paths[name]
            # No process was launched: publish the empty stream explicitly.
            if not path.exists() and not metrics.get('process_started_at'):
                try:
                    path.write_text(content, encoding='utf-8')
                except OSError as exc:
                    result.setdefault('log_integrity', {}).setdefault('errors', {})[name] = str(exc)
            if path.exists():
                result['log_refs'][name] = str(path)
            raw_preview = bytes(retained.get(name, content.encode('utf-8')))
            total_bytes = result.get('stream_bytes', {}).get(name, len(raw_preview))
            if len(raw_preview) > limit:
                head = limit // 2
                tail = limit - head
                result[name] = (raw_preview[:head].decode('utf-8', errors='replace')
                                + raw_preview[-tail:].decode('utf-8', errors='replace'))
                result['output_projection'][name] = {
                    'kind': 'head_and_tail', 'head_bytes': head, 'tail_bytes': tail,
                    'omitted_middle': True, 'full_source_ref': str(path),
                    'stream_bytes': total_bytes,
                    'included_byte_ranges': [[0, head], [total_bytes - tail, total_bytes]],
                    'omitted_byte_range': [head, total_bytes - tail],
                    'range_convention': 'zero_based_half_open',
                }
            else:
                result[name] = content
                result['output_projection'][name] = {'kind': 'complete', 'full_source_ref': str(path)}
        integrity = result.setdefault('log_integrity', {'complete': {}, 'errors': {}})
        for name, path in log_paths.items():
            integrity.setdefault('complete', {}).setdefault(name,
                not bool(metrics.get('process_started_at')) and path.exists())
            result.setdefault('log_truncated', {}).setdefault(name, not integrity['complete'][name])
        result['duration_seconds'] = time.monotonic() - started
        metrics['returned_at'] = datetime.now(timezone.utc).isoformat()
        result['receipt_ref'] = str(observation / 'receipt.json')
        self._record_metrics(task_id, result)
        # Publication is performed once by the service owner after this worker returns.
        from agent_core.cli_display import emit
        emit('details', json.dumps(result, ensure_ascii=False), task_id=task_id, node=stage,
             call_id=activity_id, purpose='telemetry')
        phase_start = started
        # Terminal activity is emitted only after the immutable receipt is published.
        if cancellation:
            raise cancellation
        return result
