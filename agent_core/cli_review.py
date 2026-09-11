"""Approval display copies; no authorization, parsing, state or input ownership."""
from dataclasses import dataclass
import hashlib
from .cli_display import emit, terminal, display_print, safe_text


@dataclass(frozen=True)
class ReviewDisplay:
    title: str
    body: str
    summary: str


def review_display(title, content, *, language='', risk='', plan_id='', step_count=None):
    body = str(content)
    identity = hashlib.sha256(body.encode('utf-8')).hexdigest()
    fields = [str(title)]
    if plan_id:
        fields.append(f'计划 {plan_id} · 步骤数 {step_count if step_count is not None else "UNKNOWN"}')
    if language:
        fields.append(f'语言 {language} · 风险检查 {risk or "UNKNOWN"}')
    fields.append('审批显示正文 SHA-256: ' + identity)
    fields.append('完整输入、输出、约束及执行作用范围见下方详情；摘要不能确认全部副作用。')
    if language:
        fields.append('工作目录与实际写入目标以完整执行定义为准；无法从现有审批字段可靠概括，需查看完整代码。')
    fields.append('/details 或 F2 查看完整对象；查看/返回不代表批准。')
    return ReviewDisplay(str(title), body, '\n'.join(fields))


def show_review(view):
    """Fallback to the full body where a trustworthy scope summary is absent."""
    emit('details', view.body, source='runtime_fact', purpose='approval_detail')
    if not emit('approval', view.summary, source='runtime_fact'):
        display_print(view.summary)
    owner = terminal()
    if owner is not None and getattr(owner, 'mode', '') == 'tty' and not getattr(owner, 'fallback', False):
        # Queue modal on the existing owner loop. The full object is available
        # before its unchanged approval transport accepts a response.
        try:
            owner.show_details(text=view.body, title=view.title)
        except Exception:
            _append_body(view)
    elif owner is None or getattr(owner, 'mode', '') != 'off':
        show_review_body(view)


def show_review_body(view):
    owner = terminal()
    if owner is not None and getattr(owner, 'mode', '') == 'tty' and not getattr(owner, 'fallback', False):
        try:
            owner.show_details(text=view.body, title=view.title)
            return
        except Exception:
            pass
    _append_body(view)


def _append_body(view):
    # Clean complete lines separately, preserving large multi-line scripts.
    # The original approval payload and its hash are never rewritten.
    for line in [view.title, *view.body.splitlines()]:
        display_print(safe_text(line))
