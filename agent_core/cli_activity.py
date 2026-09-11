"""Display copies at existing synchronous tool boundaries; no business state."""
import uuid
from .cli_display import emit


def call_with_activity(label, operation, *args, display_scope=None, display_result=None, **kwargs):
    scope = dict(display_scope or {})
    scope.setdefault('tool_call_id', 'display-tool-' + uuid.uuid4().hex)
    emit('tool_start', label + '开始', source='runtime_fact', **scope)
    try:
        result = operation(*args, **kwargs)
    except BaseException as exc:
        # Cancellation and business exceptions keep their original identity.
        emit('tool_end', label + ('已取消' if not isinstance(exc, Exception) else '失败：' + type(exc).__name__),
             source='runtime_fact', status='cancelled' if not isinstance(exc, Exception) else 'failed', **scope)
        raise
    status = 'returned'
    if display_result is not None:
        try:
            status = display_result(result)
        except Exception:
            status = 'unknown'
    suffix = {'returned': '已返回', 'empty': '已返回，未找到结果',
              'failed': '检查未通过', 'partial': '仅返回部分结果',
              'unknown': '已返回，完整性未确认'}.get(status, '已返回，状态未确认')
    emit('tool_end', label + suffix, source='runtime_fact', status=status, **scope)
    return result


def resource_display_result(result):
    if result.get('inspection_error') or (result.get('exists') and not result.get('readable')):
        return 'failed'
    return 'returned' if result.get('exists') else 'empty'


def glob_display_result(result):
    if result.get('truncated'):
        return 'partial'
    return 'returned' if result.get('matches') else 'empty'


def skill_display_result(value):
    """Observe the existing typed service receipt; never modify its contents."""
    result = value[1]
    status = result.get('status')
    if status == 'NO_MATCH':
        return 'empty'
    if status in {'CATALOG_INCOMPLETE', 'SEARCH_ERROR'}:
        return 'failed'
    if status in {'COMPLETE', 'MATCHES_FOUND'}:
        rows = result.get('results') if 'results' in result else result.get('candidates')
        if rows is not None and not rows:
            return 'empty'
        if rows and all(isinstance(item, dict) and item.get('status') == 'NO_MATCH' for item in rows):
            return 'empty'
        if any(item.get('status') not in {None, 'COMPLETE', 'OK', 'ok', 'loaded', 'AVAILABLE'}
               for item in (rows or []) if isinstance(item, dict)):
            return 'partial'
        return 'returned'
    if status == 'REQUIRES_NARROWING':
        return 'partial'
    return 'unknown'
