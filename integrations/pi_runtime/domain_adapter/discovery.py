"""Metadata-only directory snapshots under the existing file-read authority."""
import os
from pathlib import Path
import stat
import time

from agent_core.workspace_files import WorkspaceFileService
from agent_core.execution_permissions import _safe_read_info, verify_file_directory


class DiscoveryFiles(WorkspaceFileService):
    def resolve_read_path(self, value, *, task_id, call_id=None, trusted_skill_dirs=()):
        # Ordinary file operations reject an empty relative filename. Listing
        # the project (or an explicitly loaded Skill root) needs that directory.
        project, path = self.read_target(value, task_id)
        if path == project:
            return project, path, Path('.'), None, False
        if str(path) in trusted_skill_dirs:
            info = _safe_read_info(path)
            boundary = {'root': str(path), 'anchor': [info.st_dev, info.st_ino],
                        'target_kind': 'directory', 'trusted_source': True}
            return path, path, Path('.'), boundary, True
        return super().resolve_read_path(value, task_id=task_id, call_id=call_id,
                                         trusted_skill_dirs=trusted_skill_dirs)

    def list_directory(self, args, *, task_id):
        root, path, relative, boundary, _ = self.resolve_read_path(
            args.get('path'), task_id=task_id, call_id=args.get('call_id'),
            trusted_skill_dirs=args.get('trusted_skill_dirs', ()))
        with self._read_parent(root, relative, boundary) as (parent, name):
            before = os.stat(name, dir_fd=parent, follow_symlinks=False) if relative.parts else os.fstat(parent)
            self._verify_read_identity(before, boundary)
            if not stat.S_ISDIR(before.st_mode):
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise ValueError('REGULAR_SINGLE_LINK_FILE_OR_DIRECTORY_REQUIRED')
                return {'status': 'completed', 'path': str(path), 'kind': 'file', 'entries': []}
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent) if relative.parts else os.dup(parent)
            try:
                actual = os.fstat(fd)
                if (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError('FILE_PERMISSION_TARGET_REPLACED')
                verify_file_directory(path, fd)
                if Path(os.readlink(f'/proc/self/fd/{fd}')) != path:
                    raise ValueError('FILE_CHANGED_OR_OUT_OF_SCOPE')
                entries, size, scanned, limited = [], 0, 0, False
                started = time.monotonic()
                with os.scandir(fd) as iterator:
                    for entry in iterator:
                        scanned += 1
                        if scanned > 4096 or size > 256 * 1024 or time.monotonic() - started > 5:
                            limited = True
                            break
                        candidate = path / entry.name
                        try:
                            # Same private/control/runtime exclusions as read;
                            # never resolve or open a discovered symlink.
                            self.read_target(str(candidate), task_id, canonical=False)
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode):
                                kind = 'directory'
                            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                                kind = 'file'
                            else:
                                continue
                        except (OSError, ValueError):
                            continue
                        entries.append({'name': entry.name, 'kind': kind})
                        size += len(entry.name.encode('utf-8', errors='replace')) + 64
                if Path(os.readlink(f'/proc/self/fd/{fd}')) != path:
                    raise ValueError('FILE_CHANGED_OR_OUT_OF_SCOPE')
                return {'status': 'completed', 'path': str(path), 'kind': 'directory',
                        'entries': entries, 'scan_limited': limited}
            finally:
                os.close(fd)


def list_directory(service, args):
    files = DiscoveryFiles(service.commands, read_sources=service.files.read_sources)
    return files.list_directory(args, task_id=service.task_id)
