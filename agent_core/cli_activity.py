"""Display copies at existing synchronous tool boundaries; no business state."""
import uuid
from .cli_display import emit


def call_with_activity(label, operation, *args, display_scope=None, **kwargs):
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
    emit('tool_end', label + '已返回', source='runtime_fact', status='returned', **scope)
    return result
