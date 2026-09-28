"""Read one explicitly described input list. Derived rows are never Plan authority."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace


def _error(code, value='', *, field='$.input_manifest', **details):
    from agent_core.work_outputs import WorkOutputError
    return WorkOutputError(code, value, field_path=field, value=value, **details)


def project_for_state(state, project=None):
    if project is not None:
        return Path(project).absolute()
    spec = (state.get('current_task') or {}).get('task_spec') or {}
    context = spec.get('path_context') or {}
    value = context.get('project_dir') or context.get('project_root') or (state.get('plan_document') or {}).get('path_bindings', {}).get('project_dir')
    if not value or not Path(value).is_absolute():
        raise _error('INPUT_PROJECT_REQUIRED', value)
    return Path(value).absolute()


def _service(state, path, project):
    """Reuse file-service no-follow traversal; Plan entries never grant authority."""
    from agent_core.workspace_files import WorkspaceFileService
    path = Path(path).absolute()
    roots, exact = [project], []
    spec = (state.get('current_task') or {}).get('task_spec') or {}
    for row in spec.get('inputs') or []:
        if not isinstance(row, dict) or row.get('source') not in {'user', 'user_explicit', 'user_confirmed'}:
            continue
        value = row.get('canonical_path') or row.get('path')
        if not isinstance(value, str) or not Path(value).is_absolute():
            continue
        target = Path(value).absolute()
        if row.get('artifact_kind') == 'directory' or row.get('role') in {'input_dir', 'input_root', 'input_directory'}:
            roots.append(target)
        else:
            exact.append(target)
    root = next((r for r in roots if path.is_relative_to(r) and path.resolve().is_relative_to(r.resolve())), None)
    if root is None and path in exact and path.resolve() == path:
        root = path.parent  # only the already selected exact file is admitted below
    if root is None:
        raise _error('INPUT_PATH_NOT_AUTHORIZED', str(path), reason='outside the project or an existing user-authorized TaskSpec input')
    commands = SimpleNamespace(data_path=project, deny_paths=(),
        project_for_task=lambda _task: root,
        _task_dir=lambda _task: project/'task_outputs'/str(state.get('task_id') or 'manifest')/'command_inspection')
    return WorkspaceFileService(commands)


def _file(state, path, project, *, content=False):
    service = _service(state, path, project)
    try:
        root, target, relative = service._path(str(path), str(state.get('task_id') or 'manifest'))
        with service._parent(root, relative) as (parent, name):
            if content:
                data, _ = service._read(parent, name)
                if data is None:
                    raise FileNotFoundError(str(path))
                return data
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('regular single-link input file required')
                return {'path':str(target),'size':info.st_size,'mtime_ns':info.st_mtime_ns}
    except (OSError, ValueError) as exc:
        raise _error('INPUT_FILE_UNAVAILABLE', str(path), reason=str(exc)) from exc


def manifest_view(state, project=None):
    descriptor = (state.get('plan_document') or {}).get('input_manifest')
    if descriptor is None:
        return None
    if not isinstance(descriptor, dict):
        raise _error('INPUT_MANIFEST_INVALID', descriptor, reason='expected an object')
    project = project_for_state(state, project)
    value = descriptor.get('path')
    if not isinstance(value, str) or not value.strip():
        raise _error('INPUT_MANIFEST_PATH_REQUIRED', value, field='$.input_manifest.path')
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project/path
    data = _file(state, path, project, content=True)
    from agent_core.delivery_files import parse_input_manifest
    try:
        view = parse_input_manifest(data, {**descriptor,'path':str(path)})
    except (ValueError, TypeError, UnicodeError) as exc:
        raise _error('INPUT_MANIFEST_INVALID', str(path), reason=str(exc)) from exc
    for member in view['files']:
        _file(state, Path(member), project)
    return view


def manifest_snapshot(state, project=None):
    view = manifest_view(state, project)
    if view is None:
        return None
    project = project_for_state(state, project)
    fingerprints = {}
    # Preserve the existing bounded fingerprint policy. Large members are
    # explicitly prefix evidence, never a claim of full historical immutability.
    for value in [view['path'], *view['files']]:
        path = Path(value)
        service = _service(state, path, project)
        root, target, relative = service._path(str(path), str(state.get('task_id') or 'manifest'))
        with service._parent(root, relative) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise _error('INPUT_FILE_UNAVAILABLE', value)
                full = info.st_size <= 4*1024*1024
                data = stream.read(4*1024*1024 if full else 65536)
                after=os.fstat(stream.fileno())
                if (after.st_size,after.st_mtime_ns)!=(info.st_size,info.st_mtime_ns):
                    raise _error('INPUT_MEMBER_CHANGED',value,reason='changed while fingerprinting')
                fingerprints[value] = {'path':value,'size':info.st_size,'mtime_ns':info.st_mtime_ns,
                    'sha256' if full else 'sample_sha256':hashlib.sha256(data).hexdigest(),
                    'hash_coverage_bytes':len(data),'hash_coverage':'full' if full else 'prefix'}
    if fingerprints[view['path']].get('sha256') != view['sha256']:
        raise _error('INPUT_MANIFEST_CHANGED', view['path'], reason='changed while reading')
    return {k:view[k] for k in ('path','sha256','version','descriptor')} | {'files':fingerprints}


def manifest_fingerprints(state, project=None):
    snapshot = manifest_snapshot(state, project)
    return snapshot['files'] if snapshot else {}


def validate_manifest_unchanged(snapshot, state, project=None):
    current = manifest_snapshot(state, project)
    if snapshot is None and current is None:
        return None
    if not isinstance(snapshot, dict) or not current or any(snapshot.get(k) != current[k] for k in ('path','sha256','version','descriptor')):
        raise _error('INPUT_MANIFEST_CHANGED', (current or {}).get('path',''), reason='the consumed input list or its interpretation changed')
    if snapshot.get('files') != current['files']:
        changed = sorted(k for k in set(snapshot.get('files') or {}) | set(current['files']) if (snapshot.get('files') or {}).get(k) != current['files'].get(k))
        raise _error('INPUT_MEMBER_CHANGED', current['path'], paths=changed, reason='known member version change; prefix hashes do not prove full immutability')
    return current
