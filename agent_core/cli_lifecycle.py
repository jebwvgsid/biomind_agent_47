"""Display projections of supplied receipts. No state loading or business decisions."""
import json


def tool_result_text(tool, observation):
    result = observation.get('result') or {}
    if not isinstance(result, dict):
        result = {}
    error = observation.get('error') or {}
    ref = str(result.get('path') or result.get('ref_id') or observation.get('source_ref') or '')
    if len(ref) > 160:
        ref = str((result.get('ref') or {}).get('scope', {}).get('object_kind') or '已登记证据')
    label = tool + (' · ' + ref if ref else '')
    if error:
        return label + '：读取失败 ' + str(error.get('code') or 'UNKNOWN'), 'failed'
    status = str(result.get('status') or '')
    if status == 'REQUIRES_NARROWING':
        return label + '：查询范围过大，需要缩小范围', 'partial'
    if status == 'RECOVERY_OBSERVATION_BUDGET_EXCEEDED':
        return label + '：工具内容未进入当前响应，观测预算已达上限', 'partial'
    facts = []
    if type(result.get('start_line')) is int and type(result.get('end_line')) is int:
        facts.append(f"返回第 {result['start_line']}–{result['end_line']} 行")
    for key in ('returned_count', 'returned'):
        if type(result.get(key)) is int:
            facts.append(f"返回 {result[key]} 项"); break
    cursor = result.get('cursor') or {}
    if result.get('truncated') and type(cursor.get('next_start_line')) is int:
        facts.append(f"分页；可从第 {cursor['next_start_line']} 行继续")
    elif result.get('truncated'):
        facts.append('工具内容有裁剪；续读位置未提供')
    if observation.get('truncated') and not result.get('truncated'):
        facts.append('模型可见工具内容已裁剪')
        if result.get('observation_ref'):
            facts.append('完整观测已归档，可按回执引用读取')
    if status == 'NO_MATCH':
        facts.append('未找到匹配项')
    if not facts:
        facts.append('已返回')
    return label + '：' + '；'.join(facts), ('partial' if observation.get('truncated') or result.get('truncated') else 'empty' if status == 'NO_MATCH' else 'returned')


def terminal_text(status, facts):
    stop = facts.get('stop_reason') or {}
    origin = facts.get('origin') or facts.get('primary_cause') or {}
    rows = [f"任务已停止 · {status}" if status != 'completed' else '任务正式完成']
    completed = facts.get('prior_completed_work')
    if isinstance(completed, list):
        rows.append(f'已有 {len(completed)} 步正式提交，完成事实保留。')
    original = origin.get('error_code') or origin.get('reason_code')
    if original:
        rows.append('原始问题：' + str(original))
    failures = list(dict.fromkeys(str(r.get('error_code') or 'UNKNOWN') for r in facts.get('inspection_failures', [])))
    if failures:
        rows.append('恢复取证问题：' + '、'.join(failures))
    trigger = stop.get('error_code') or facts.get('error_code')
    if trigger:
        rows.append('停止触发：' + str(trigger))
    usage = facts.get('recovery_usage') or {}
    if usage.get('scope') in {'last_admitted_episode', 'current_episode'}:
        label = '最近已记录调查' if usage['scope'] == 'last_admitted_episode' else '本次调查'
        rows.append(f"{label}：{usage.get('turn_count', '未知')} 次模型请求，{usage.get('tool_call_count', '未知')} 次工具调用；不是全会话累计。")
    else:
        rows.append('调查累计次数：未知（现有回执未提供完整范围）。')
    rows.append(delivery_text(facts))
    if facts.get('summary_llm_calls') == 0:
        rows.append('系统事实摘要；模型总结未运行。')
    elif facts.get('summary_llm_calls') is None:
        rows.append('系统事实摘要；模型总结运行情况未记录。')
    return '\n'.join(rows)


def delivery_text(facts):
    receipts = facts.get('report_receipts') or {}
    text = '文件交付：' + str(facts.get('required_output_status') or facts.get('delivery_status') or 'unknown')
    if any(row.get('reason') == 'RUNTIME_REPORT_EXISTING_FILE_PROTECTED' for row in receipts.values()):
        text += '；目标文件受保护，未覆盖'
    internal = facts.get('internal_diagnostic_delivery') or {}
    if internal.get('status') not in {None, '', 'not_needed'}:
        text += '；内部诊断归档=' + str(internal['status'])
    return text


def delivery_identity(facts):
    return json.dumps({key: facts.get(key) for key in (
        'report_receipts', 'required_output_status', 'delivery_status', 'internal_diagnostic_delivery')}, sort_keys=True, default=str)
