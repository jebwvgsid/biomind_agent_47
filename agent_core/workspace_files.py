"""Bounded text-file operations for the agent, separate from running a script."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from agent_core.command_execution import _PRIVATE_PATTERN


MAX_FILE_BYTES = 1024 * 1024
MAX_WRITE_BYTES = 128 * 1024


def _schema(name, description, properties, required):
    return {'type': 'function', 'function': {
        'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': required, 'additionalProperties': False},
    }}


READ_FILE_SCHEMA = _schema('read_file',
    'Read a UTF-8 project text file and its full SHA-256 for a subsequent edit. '
    'Paths are project files or exact host-issued read-only sources. Follow next_offset_chars with '
    'expected_sha256 to read long JSON completely; offsets count Unicode characters. Symlinks/private files excluded.',
    {'path': {'type': 'string'}, 'start_line': {'type': 'integer', 'minimum': 1},
     'max_lines': {'type': 'integer', 'minimum': 1, 'maximum': 400},
     'offset_chars': {'type': 'integer', 'minimum': 0},
     'max_chars': {'type': 'integer', 'minimum': 1, 'maximum': 6000},
     'expected_sha256': {'type': 'string'}}, ['path'])
WRITE_FILE_SCHEMA = _schema('write_file',
    'Create or replace a UTF-8 project file (at most 128 KiB), without executing it. '
    'For new files expected_sha256 must be empty; for existing files first use read_file '
    'and supply the returned hash. Parent directories are created. Returns path, hash and edit receipt.',
    {'path': {'type': 'string'}, 'content': {'type': 'string'},
     'expected_sha256': {'type': 'string'}}, ['path', 'content', 'expected_sha256'])


def _digest(value):
    return hashlib.sha256(value).hexdigest()


class WorkspaceFileService:
    def __init__(self, command_service, *, draft_root=None, read_sources=None):
        self.commands = command_service
        self.read_sources = dict(read_sources or {})
        # Only program-selected draft directories may enter task_outputs. The
        # ordinary project file tools retain their original protection policy.
        self.draft_root = Path(draft_root).absolute() if draft_root is not None else None

    def _path(self, value, task_id):
        if not isinstance(value, str) or not value.strip() or '\x00' in value:
            raise ValueError('FILE_PATH_REQUIRED')
        project = self.draft_root or self.commands.project_for_task(task_id)
        supplied = Path(value).expanduser()
        if '..' in supplied.parts:
            raise ValueError('FILE_PATH_TRAVERSAL')
        path = supplied if supplied.is_absolute() else project / supplied
        try:
            relative = path.relative_to(project)
        except ValueError as exc:
            raise ValueError('FILE_OUTSIDE_PROJECT: select the intended --project-dir') from exc
        if not relative.parts:
            raise ValueError('FILE_PATH_REQUIRED')
        if any(_PRIVATE_PATTERN.fullmatch(part.lower()) or part.lower() in {'auth.json', '启动.sh', 'privatekeys', '.biomind'}
               for part in relative.parts):
            raise ValueError('PRIVATE_FILE_EXCLUDED')
        runtime_root = self.commands.data_path / 'task_outputs'
        own_root = self.commands._task_dir(task_id).parent
        if path.is_relative_to(runtime_root) and not path.is_relative_to(own_root):
            raise ValueError('PROTECTED_RUNTIME_PATH: another task')
        protected = [*(() if self.draft_root is not None else (self.commands.data_path / 'task_outputs',)),
                     *getattr(self.commands, 'deny_paths', ())]
        if any(path == root or path.is_relative_to(root) for root in protected):
            raise ValueError('PROTECTED_RUNTIME_PATH')
        return project, path, relative

    @contextmanager
    def _parent(self, project, relative, *, create=False):
        # Walk descriptors, never resolve/follow a model-supplied symlink.
        from agent_core.execution_permissions import verify_file_directory, may_create_file_directory
        fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        current = project
        try:
            verify_file_directory(current, fd)
            for part in relative.parts[:-1]:
                current = current / part
                if create and may_create_file_directory(current):
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                verify_file_directory(current, fd)
            yield fd, relative.name
        finally:
            os.close(fd)

    @staticmethod
    def _read(parent, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None, None
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('REGULAR_SINGLE_LINK_FILE_REQUIRED')
            if info.st_size > MAX_FILE_BYTES:
                raise ValueError('FILE_TOO_LARGE: use exec_command for bounded inspection')
            data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError('FILE_TOO_LARGE')
        data.decode('utf-8')
        return data, stat.S_IMODE(info.st_mode)

    def stat(self, args, *, task_id, trusted_skill_dirs=()):
        """Metadata only. Skill directories come from Pi's trusted catalog, not model args."""
        value = args.get('path')
        supplied = Path(value).expanduser() if isinstance(value, str) else Path('.')
        if not isinstance(value, str) or not value.strip() or '\x00' in value or '..' in supplied.parts:
            raise ValueError('FILE_PATH_REQUIRED')
        if any(_PRIVATE_PATTERN.fullmatch(part.lower()) or part.lower() in {
                'auth.json', '启动.sh', 'privatekeys', '.biomind'} for part in supplied.parts):
            raise ValueError('PRIVATE_FILE_EXCLUDED')
        external = supplied.is_absolute() and (str(supplied) in self.read_sources or any(
            supplied.is_relative_to(Path(root)) for root in trusted_skill_dirs))
        if external:
            path = supplied
            project, relative = Path(path.anchor), path.relative_to(path.anchor)
        else:
            project, path, relative = self._path(value, task_id)
        with self._parent(project, relative) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('REGULAR_SINGLE_LINK_FILE_REQUIRED')
        return {'status': 'completed', 'path': str(path), 'bytes': info.st_size,
                'device': info.st_dev, 'inode': info.st_ino, 'mtime_ns': str(info.st_mtime_ns),
                'mode': stat.S_IMODE(info.st_mode), 'read_only_source': bool(external),
                'expected_sha256': self.read_sources.get(str(path))}

    def read(self, args, *, task_id):
        value = args.get('path')
        pinned = self.read_sources.get(value) if isinstance(value, str) else None
        if pinned:
            path = Path(value)
            if not path.is_absolute() or '..' in path.parts or any(
                    _PRIVATE_PATTERN.fullmatch(part.lower()) for part in path.parts):
                raise ValueError('PRIVATE_OR_INVALID_SOURCE')
            project, relative = Path(path.anchor), path.relative_to(path.anchor)
        else:
            project, path, relative = self._path(value, task_id)
        first, count = args.get('start_line', 1), args.get('max_lines', 200)
        if type(first) is not int or first < 1 or type(count) is not int or not 1 <= count <= 400:
            raise ValueError('INVALID_LINE_RANGE')
        with self._parent(project, relative) as (parent, name):
            data, _ = self._read(parent, name)
        if data is None:
            raise FileNotFoundError(str(path))
        digest = _digest(data)
        expected = args.get('expected_sha256') or pinned
        if expected and expected != digest:
            raise ValueError('FILE_CHANGED: restart the read using the current host locator')
        text = data.decode('utf-8')
        lines = text.splitlines(keepends=True)
        offset = args.get('offset_chars', sum(len(line) for line in lines[:first - 1]))
        limit = args.get('max_chars', 6000)
        if type(offset) is not int or not 0 <= offset <= len(text) or type(limit) is not int or not 1 <= limit <= 6000:
            raise ValueError('INVALID_CHARACTER_RANGE')
        line_end = sum(len(line) for line in lines[:first - 1 + count])
        end = min(len(text), offset + limit, line_end if 'offset_chars' not in args else len(text))
        end = max(offset, end)
        complete = end == len(text)
        return {'status': 'completed', 'path': str(path), 'sha256': digest,
                'content': text[offset:end], 'start_line': first, 'total_lines': len(lines),
                'offset_chars': offset, 'next_offset_chars': None if complete else end,
                'total_chars': len(text), 'complete': complete,
                'next_line': None if complete else text[:end].count('\n') + 1,
                'truncated': {'content': not complete}, 'bytes': len(data)}

    def write(self, args, *, task_id):
        project, path, relative = self._path(args.get('path'), task_id)
        content, expected = args.get('content'), args.get('expected_sha256')
        if not isinstance(content, str) or not isinstance(expected, str):
            raise ValueError('CONTENT_AND_EXPECTED_SHA256_REQUIRED')
        data = content.encode('utf-8')
        if len(data) > MAX_WRITE_BYTES or '\x00' in content:
            raise ValueError('TEXT_WRITE_TOO_LARGE_OR_BINARY')
        operation = self.commands._task_dir(task_id).parent / 'file_edits' / uuid.uuid4().hex
        with self._parent(project, relative, create=True) as (parent, name):
            # Serialize BioMind writers, including across processes.
            fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before, mode = self._read(parent, name)
            previous = _digest(before) if before is not None else ''
            if expected != previous:
                raise ValueError('FILE_CHANGED: read_file again before replacing existing content')
            operation.mkdir(parents=True, exist_ok=False)
            if before is not None:
                (operation / 'before.txt').write_bytes(before)
            (operation / 'after.txt').write_bytes(data)
            receipt = {'status': 'prepared', 'operation': 'write_file', 'path': str(path),
                       'before_sha256': previous, 'sha256': _digest(data), 'bytes': len(data),
                       'created': before is None, 'executed': False,
                       'receipt_ref': str(operation / 'receipt.json')}
            def record():
                target = operation / 'receipt.json'
                temporary = operation / 'receipt.tmp'
                temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
                temporary.replace(target)
            record()
            temporary_name = '.biomind-edit-' + uuid.uuid4().hex
            try:
                fd = os.open(temporary_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), (mode & 0o777) if mode is not None else 0o644)
                    os.fsync(stream.fileno())
                current, _ = self._read(parent, name)
                if current != before:
                    raise ValueError('FILE_CHANGED: target changed during edit')
                if before is None:
                    # A new file must never overwrite a concurrently created file.
                    os.link(temporary_name, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                    os.unlink(temporary_name, dir_fd=parent)
                else:
                    os.replace(temporary_name, name, src_dir_fd=parent, dst_dir_fd=parent)
                receipt['status'] = 'completed'
                record()
                from agent_core.cli_tool_activity import committed_file
                committed_file(receipt, before, data)
                return receipt
            except Exception as exc:
                receipt.update(status='failed', error=str(exc))
                record()
                raise
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def mutation_snapshot(self, args, *, task_id):
        """Host-only input for the native Pi edit algorithm; never creates parents.

        Existing descriptor walks and size/private-file rules remain the single
        filesystem boundary. A later approved write repeats the CAS under lock.
        """
        project, path, relative = self._path(args.get('path'), task_id)
        try:
            with self._parent(project, relative) as (parent, name):
                data, _ = self._read(parent, name)
        except FileNotFoundError:
            data = None
        return {'path': str(path), 'exists': data is not None,
                'sha256': _digest(data) if data is not None else '',
                'content': data.decode('utf-8') if data is not None else ''}

    def read_complete(self, path, *, task_id):
        """Read a bounded current file for validation, without a model excerpt."""
        project, resolved, relative = self._path(path, task_id)
        with self._parent(project, relative) as (parent, name):
            data, _ = self._read(parent, name)
        if data is None:
            raise FileNotFoundError(str(resolved))
        return {'path': str(resolved), 'sha256': _digest(data),
                'content': data.decode('utf-8'), 'bytes': len(data)}

    def patch(self, args, *, task_id):
        """One exact replacement; write's existing CAS also closes the read race."""
        old, new = args.get('old_text'), args.get('new_text')
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError('NONEMPTY_OLD_TEXT_AND_NEW_TEXT_REQUIRED')
        current = self.read_complete(args.get('path'), task_id=task_id)
        if args.get('expected_sha256') != current['sha256']:
            raise ValueError('FILE_CHANGED: read the current draft before patching')
        matches = current['content'].count(old)
        if matches != 1:
            reason = 'PATCH_TARGET_NOT_FOUND' if not matches else 'PATCH_TARGET_NOT_UNIQUE'
            raise ValueError(f'{reason}: matches={matches}; read the relevant lines and include unique context')
        return self.write({'path': current['path'], 'expected_sha256': current['sha256'],
                           'content': current['content'].replace(old, new, 1)}, task_id=task_id)

    async def dispatch(self, name, args, *, task_id, call_id=None):
        try:
            if not isinstance(args, dict):
                raise ValueError('FILE_ARGUMENTS_MUST_BE_OBJECT')
            if name == 'read_file':
                return self.read(args, task_id=task_id)
            if name in {'write_file','patch_file'}:
                from agent_core.execution_permissions import authorize_file, file_dispatch_context, claim_permission, complete_permission
                import uuid
                identity=call_id or 'file:'+uuid.uuid4().hex
                permission = await authorize_file(self, name, args, task_id=task_id, call_id=identity)
                if permission.get('result'):
                    return {**permission['result'],'replayed':True}
                if permission.get('claim'):
                    return {'status':'reconciliation_required','execution_started':None,
                            'error':'FILE_CLAIM_OUTCOME_UNKNOWN','retryable_in_current_scope':False,
                            'next_actions':['read_file']}
                permit=claim_permission(self.commands.data_path,task_id,permission,identity)
                with file_dispatch_context(permit):
                    result=self.write(args, task_id=task_id) if name=='write_file' else self.patch(args, task_id=task_id)
                complete_permission(permit,result)
                return result
            raise ValueError('UNKNOWN_FILE_TOOL')
        except (OSError, ValueError) as exc:
            return {'status': 'not_run', 'error': str(exc), 'error_type': type(exc).__name__}

    def verify(self, artifacts, *, task_id):
        """Only actual, still-current edit receipts become delivered artifacts."""
        verified = []
        for artifact in artifacts:
            current = self.read({'path': artifact['path'], 'max_lines': 1}, task_id=task_id)
            if current['sha256'] != artifact['sha256']:
                raise ValueError(f"DELIVERED_FILE_CHANGED: {artifact['path']}")
            verified.append(dict(artifact))
        return verified
