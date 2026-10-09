"""Display copies at existing synchronous tool boundaries; no business state."""
import uuid
from .cli_display import emit, safe_text


def _single_line_summary(value, limit):
    # Sanitize before truncating so a cut credential cannot escape redaction.
    text = ' '.join(safe_text(value).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _failure_summary(exc):
    # Keep Skill/config imports out of CLI startup; display failures must not
    # cause configuration to be captured before the application initializes.
    from .skill_access import SkillAccessError

    summary = type(exc).__name__
    if isinstance(exc, SkillAccessError):
        code = _single_line_summary(exc.code, 96)
        detail = _single_line_summary(exc.detail, 240)
        if code:
            summary += ' [' + code + ']'
        if detail:
            summary += '：' + detail
    return summary


def call_with_activity(label, operation, *args, display_scope=None, display_result=None,
                       display_arguments=None, **kwargs):
    from .cli_tool_activity import activity, current
    if current() is None:
        observer = activity(label, display_arguments, scope=display_scope)
        with observer.dispatch():
            result = _call_with_activity(label, operation, *args, display_scope=display_scope,
                display_result=display_result, **kwargs)
        receipt = result if isinstance(result, dict) else result[1] if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict) else {}
        observer.event('returned', receipt)
        return result
    return _call_with_activity(label, operation, *args, display_scope=display_scope,
                               display_result=display_result, **kwargs)


def _call_with_activity(label, operation, *args, display_scope=None, display_result=None, **kwargs):
    scope = dict(display_scope or {})
    scope.setdefault('tool_call_id', 'display-tool-' + uuid.uuid4().hex)
    emit('tool_start', label + '开始', source='runtime_fact', **scope)
    try:
        result = operation(*args, **kwargs)
    except BaseException as exc:
        # Cancellation and business exceptions keep their original identity.
        emit('tool_end', label + ('已取消' if not isinstance(exc, Exception) else '失败：' + _failure_summary(exc)),
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
    if status in {'PARTIAL', 'REQUIRES_NARROWING'}:
        return 'partial'
    return 'unknown'
