"""Best-effort, display-only tool receipts. Never consulted by dispatch/approval.

Context travels with the actual dispatch coroutine (and its owned tasks), not
the last visible model call. The temporary archive is not a recovery journal.
"""
from contextvars import ContextVar
from contextlib import contextmanager
import difflib
import re
import uuid
import os
from pathlib import Path

_current = ContextVar('biomind_tool_display', default=None)
_ARGS = {'cmd', 'command', 'argv', 'cwd', 'path', 'paths', 'name', 'query', 'pattern',
         'skill_id', 'skill_ids', 'section', 'sections', 'start_line', 'max_lines',
         'run_ref', 'max_wait_ms', 'timeout_seconds', 'yield_time_ms', 'role'}
_ARGS.update({'requests', 'operation', 'resource_path', 'snapshot_id', 'char_offset', 'start_char', 'end_char'})
_ROW_FIELDS = {'skill_id', 'snapshot_id', 'operation', 'resource_path', 'path', 'name',
               'status', 'char_offset', 'start_line', 'end_line', 'has_more', 'body_delivered'}
_RESULT = {'status', 'error', 'error_type', 'exit_code', 'returncode', 'terminal',
           'path', 'cwd', 'run_ref', 'attempt_id', 'execution_attempt_id', 'pid',
           'sha256', 'before_sha256', 'bytes', 'created', 'executed', 'execution_started',
           'start_line', 'next_line', 'total_lines', 'truncated', 'observation_source',
           'elapsed_seconds', 'output_idle_seconds', 'remaining_seconds', 'cancel_requested'}
_RESULT.add('cleanup_confirmed')
_RESULT.update({'cmd', 'argv', 'environment_names', 'action_id'})
_RESULT.update({'results', 'candidates', 'matches'})
_SCOPE = {'task_id', 'turn_ref', 'user_turn', 'semantic_invocation_id', 'attempt_id',
          'attempt_ordinal', 'provider_attempt_id', 'source_message_id', 'node',
          'tool_call_id', 'tool_request_id', 'epoch', 'seed_id', 'step_id', 'action_id'}


def clean(value, limit=32768):
    from .cli_display import safe_text
    text = safe_text(value)
    # Shell flags and URL userinfo are display copies, never executable argv.
    text = re.sub(r'(?i)(--?(?:api[-_]key|token|password|passwd|secret)\s+)(?:"[^"\n]*"|\x27[^\x27\n]*\x27|\S+)',
                  r'\1[REDACTED]', text)
    text = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', text)
    return text[:limit] + ('…[显示副本已截断]' if len(text) > limit else '')


def projection(value, fields):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in sorted(fields & value.keys()):
        item = value[key]
        if item is None or isinstance(item, (bool, int, float)):
            result[key] = item
        elif isinstance(item, str):
            result[key] = clean(item, 32768 if key in {'cmd', 'command'} else 2048)
        elif isinstance(item, (list, tuple)):
            result[key] = [projection(v, _ROW_FIELDS) if isinstance(v, dict) else clean(v, 512)
                           for v in item[:8] if isinstance(v, (dict, str, int, float))]
            if len(item) > 8:
                result[key + '_omitted'] = len(item) - 8
        elif key == 'truncated':
            result[key] = True if item else False
    return result


class ToolActivity:
    def __init__(self, name, arguments=None, *, scope=None, request_scope=''):
        self.scope = projection(dict(scope or {}), _SCOPE)
        # No native identity: local display ID explicitly has no business meaning.
        native = self.scope.get('tool_call_id') or self.scope.get('tool_request_id')
        self.identity = (str(self.scope.get('task_id') or ''), str(request_scope),
                         str(self.scope.get('source_message_id') or ''),
                         str(native or 'display-only-' + uuid.uuid4().hex))
        self.name = clean(name, 128)
        self.arguments = projection(arguments, _ARGS)

    def event(self, phase, result=None, **extra):
        try:
            from .cli_display import emit
            payload = projection(result, _RESULT)
            bodies = {}
            if isinstance(result, dict):
                for key in ('stdout', 'stderr', 'stdout_delta', 'stderr_delta', 'content'):
                    if isinstance(result.get(key), str):
                        # Explicit bounded model-facing receipt; never open raw log paths.
                        bodies[key] = clean(result[key], 32768)
                if 'content' not in bodies and isinstance(result.get('results'), list):
                    bodies['content'] = '\n\n'.join(clean(row.get('exact_text', ''), 4096)
                        for row in result['results'][:8] if isinstance(row, dict))
            emit('tool_activity', '', source='runtime_fact', **self.scope,
                 tool_event={'identity': self.identity, 'name': self.name,
                     'arguments': self.arguments, 'phase': phase, 'result': payload,
                     'bodies': bodies, **extra})
        except Exception:
            pass

    @contextmanager
    def dispatch(self):
        token = _current.set(self)
        self.event('dispatch')
        try:
            yield
        except BaseException as exc:
            import asyncio
            self.event('cancelled_wait' if isinstance(exc, asyncio.CancelledError) else 'unknown',
                       {'error_type': type(exc).__name__})
            raise
        finally:
            _current.reset(token)


def activity(name, arguments=None, *, scope=None, request_scope=''):
    """Construction failure, like notification failure, cannot fail an action."""
    try:
        return ToolActivity(name, arguments, scope=scope, request_scope=request_scope)
    except Exception:
        return _NullActivity()


class _NullActivity:
    def event(self, *args, **kwargs): pass
    @contextmanager
    def dispatch(self): yield


def current():
    return _current.get()


def runtime_event(phase, result):
    try:
        observer = current()
        if observer is not None:
            observer.event(phase, result)
    except Exception:
        pass


@contextmanager
def _artifact_fd(path):
    """Open a producer-registered log without following any symlink component."""
    path = Path(path)
    if not path.is_absolute() or path.name not in {'stdout.log', 'stderr.log'} or '..' in path.parts:
        raise ValueError('unregistered log type')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    fd = None
    try:
        for part in path.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory); directory = following
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        import stat
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('not an owned regular log')
        yield fd
    finally:
        if fd is not None: os.close(fd)
        os.close(directory)


def _version(fd):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def register_owner_logs(owner):
    """Called by the real owner after terminal persistence/cleanup, never by a model result."""
    try:
        observer = current()
        if observer is None or not owner.result or not owner.result.get('terminal') or not owner.result.get('cleanup_confirmed'):
            return
        refs = {}
        for name in ('stdout', 'stderr'):
            path = Path(owner.directory) / (name + '.log')
            try:
                with _artifact_fd(path) as fd:
                    refs[name] = {'path': str(path), 'version': _version(fd), 'type': 'owned_terminal_log'}
            except (OSError, ValueError):
                continue
        observer.event('references', artifact_refs=refs)
    except Exception:
        pass


def artifact_page(ref, page):
    """Bounded byte pages, whole-line redaction before display; never arbitrary paths."""
    try:
        if ref.get('type') != 'owned_terminal_log':
            raise ValueError('invalid reference')
        with _artifact_fd(ref['path']) as fd:
            if _version(fd) != ref['version']:
                return '日志版本已变化；未展示替换后的内容。'
            offset = page * 4096
            block = os.pread(fd, 6144, offset)
            if not block:
                return '已到日志末页。'
            position = 0
            if offset and os.pread(fd, 1, offset-1) != b'\n':
                # The previous byte page owns this line. Very long lines are
                # omitted, not split across redaction boundaries.
                end = block.find(b'\n')
                if end < 0: return '长行超过显示预算；本页省略。'
                position = end + 1
            output = []
            while position < min(4096, len(block)):
                end = block.find(b'\n', position)
                if end < 0:
                    end = len(block)
                    complete = offset + end == ref['version'][2]
                else:
                    end += 1; complete = True
                line = block[position:end]
                output.append(clean(line.decode('utf-8', errors='replace'), 2048)
                              if complete and len(line) <= 2048 else '[长行超过显示预算；已省略]\n')
                position = end
            if _version(fd) != ref['version']:
                return '读取期间日志发生变化；本页未展示。'
            return ''.join(output) or '本页由上一页跨界行覆盖；请继续翻页。'
    except (OSError, ValueError, KeyError):
        return '已登记日志不可用、归属不明或被替换；未读取其他来源。'


def committed_file(receipt, before, after):
    """Only called after the existing completed receipt write succeeds."""
    try:
        observer = current()
        if observer is None:
            return
        if len(before or b'') + len(after) > 32768:
            observer.event('file_receipt', receipt, diff='文件已保存；差异超过显示预算。')
            return
        old = (before or b'').decode('utf-8').splitlines(keepends=True)
        new = after.decode('utf-8').splitlines(keepends=True)
        if len(old) + len(new) > 500:
            observer.event('file_receipt', receipt, diff='文件已保存；差异超过行数预算。')
            return
        lines = list(difflib.unified_diff(old, new, fromfile='保存前', tofile='已保存版本'))
        added = sum(line.startswith('+') and not line.startswith('+++') for line in lines)
        removed = sum(line.startswith('-') and not line.startswith('---') for line in lines)
        observer.event('file_receipt', receipt, diff=clean(''.join(lines), 32768),
                       edit_summary=f'+{added} −{removed}')
    except Exception:
        pass


