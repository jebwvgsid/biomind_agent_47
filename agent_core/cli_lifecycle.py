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
    limits = []
    if result.get('diagnostic_only') is True:
        limits.append('仅诊断')
    if result.get('source_uncertain') is True:
        limits.append('文件来源尚未确认')
    if limits:
        label += '（' + '，'.join(limits) + '）'
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


def _brief(value, limit=180):
    return ' '.join(str(value or '').split())[:limit]


_OUTPUT_PROTECTION = {
    'OUTPUT_TARGET_CHANGED_SINCE_REVIEW': (
        '目标文件在审批后发生变化，原审批不能用于当前文件',
        '重新检查目标文件当前内容，再审阅并批准新的候选动作。'),
    'OUTPUT_REPLACEMENT_NOT_APPROVED': (
        '替换现有输出文件尚未获得批准',
        '查看需要替换的文件和候选动作，明确批准替换后再继续。'),
    'OUTPUT_BACKUP_OR_RECEIPT_FAILED': (
        '旧输出备份或备份回执未确认成功',
        '查看备份与回执详情，确认旧文件已保留并补齐保护条件后再继续。'),
}


def _effect_identity_issue(facts):
    """Decode the recorded serializer exception of this causal attempt only."""
    origin, execution = facts.get('origin') or {}, facts.get('execution') or {}
    if (origin.get('error_code') != 'EXECUTION_FAILED'
            or not origin.get('attempt_id')
            or origin['attempt_id'] != execution.get('attempt_id')
            or type(execution.get('returncode')) is not int
            or execution['returncode'] == 0):
        return {}
    # Only a final structured exception is a diagnosis. A code mentioned in
    # a script, an earlier caught exception or a tool error is not this cause.
    for line in str(execution.get('stderr') or '').rstrip().splitlines()[-1:]:
        if not line.startswith('ValueError: '):
            continue
        try:
            issue = json.loads(line.removeprefix('ValueError: '))
        except (ValueError, TypeError):
            continue
        if (not isinstance(issue, dict)
                or issue.get('code') != 'EFFECT_FACT_IDENTITIES_MISMATCH'
                or issue.get('field_path') not in {
                    '$.facts.produces_artifacts', '$.facts.produces_observations',
                    '$.facts.explicit_side_effects'}
                or any(not isinstance(issue.get(key), list)
                       or not all(isinstance(item, str) and item for item in issue[key])
                       for key in ('actual', 'expected'))):
            continue
        if set(issue['actual']) != set(issue['expected']):
            return issue
    return {}


def _cause_text(facts, origin):
    issue = _effect_identity_issue(facts)
    if issue:
        extra = sorted(set(issue['actual']) - set(issue['expected']))
        missing = sorted(set(issue['expected']) - set(issue['actual']))
        parts = []
        if extra:
            parts.append('证据登记含未声明的标识 ' + _brief('、'.join(extra)))
        if missing:
            parts.append('证据登记缺少声明的标识 ' + _brief('、'.join(missing)))
        declared = _brief('、'.join(issue['expected'])) or '无'
        return '；'.join(parts) + '（声明标识：' + declared + '；EFFECT_FACT_IDENTITIES_MISMATCH）。', True
    code = str(origin.get('error_code') or origin.get('reason_code') or '')
    if code in _OUTPUT_PROTECTION:
        return _OUTPUT_PROTECTION[code][0] + '（' + code + '）。', False
    if code == 'EXECUTION_FAILED':
        rc = origin.get('returncode')
        label = '脚本执行失败' + (f'，退出码 {rc}' if type(rc) is int else '')
        return label + '；具体原因尚未定位（EXECUTION_FAILED）。', False
    labels = {'DELIVERY_CONTRACT_UNMET': '交付条件尚未满足',
              'WORKSPACE_MUTATION_NOT_ATTESTED': '工作区变更未通过正式核验',
              'WORKSPACE_MUTATION_RECEIPT_INVALID': '工作区变更核验回执无效'}
    if code in labels:
        return labels[code] + '（' + code + '）。', False
    return ('尚未定位具体原因（' + code + '）。' if code else '具体原因尚未定位。'), False


def _stop_text(stop, code):
    if code in {'RECOVERY_NO_PROGRESS', 'RECOVERY_NO_PROGRESS_LIMIT'}:
        rounds = stop.get('no_progress_rounds')
        prefix = f'连续 {rounds} 轮' if type(rounds) is int and rounds > 0 else '恢复调查'
        return prefix + '未取得可记录进展，达到停止条件；自动纠错已停止（' + code + '）。'
    descriptions = {
        'RECOVERY_BUDGET_EXHAUSTED': '恢复调查次数预算已耗尽，自动纠错已停止',
        'RECOVERY_RESOURCE_LIMIT': '恢复调查累计资源达到上限，自动纠错已停止',
        'RECOVERY_PROVIDER_TRANSPORT_FAILED': '恢复请求传输失败，已结束本次自动调查',
    }
    return descriptions.get(code, '已按记录的停止条件结束；具体触发细节见详情') + '（' + code + '）。'


def terminal_text(status, facts):
    stop = facts.get('stop_reason') or {}
    origin = facts.get('origin') or facts.get('primary_cause') or {}
    rows = [f"任务已停止 · {status}" if status != 'completed' else '任务正式完成']
    completed = facts.get('prior_completed_work')
    if isinstance(completed, list):
        rows.append(f'已有 {len(completed)} 步正式提交，完成事实保留。')
    stages = {row.get('step_id'): row for row in facts.get('stages', []) if isinstance(row, dict)}
    execution = facts.get('execution') or {}
    current = execution.get('step_id') or (facts.get('scope') or {}).get('step_id')
    if isinstance(completed, list) and completed:
        names = [_brief((stages.get(row.get('step_id')) or {}).get('title') or row.get('step_id'), 60)
                 for row in completed if isinstance(row, dict)]
        rows.append('已提交步骤：' + '；'.join(names[:5]) + ('；其余见详情' if len(names) > 5 else ''))
    if current:
        progress = '已正式提交' if execution.get('committed') is True else '尚未正式提交'
        rows.append('当前步骤：' + _brief((stages.get(current) or {}).get('title') or current) + '（' + progress + '）。')
    cause, identity_issue = _cause_text(facts, origin)
    if status != 'completed':
        rows.append('原始问题：' + cause)
    failures = list(dict.fromkeys(str(r.get('error_code') or 'UNKNOWN') for r in facts.get('inspection_failures', [])))
    if failures:
        labels = {'ARTIFACT_LOCATOR_MISSING': '未取得产物的可读位置',
                  'ARTIFACT_PERMISSION_DENIED': '读取产物时权限不足',
                  'FILE_NOT_FOUND': '读取时目标文件不存在'}
        rows.append('恢复取证问题：' + '；'.join(labels.get(code, '读取证据失败') + '（' + code + '）' for code in failures))
    trigger = stop.get('error_code') or facts.get('error_code')
    if trigger:
        rows.append('停止触发：' + _stop_text(stop, str(trigger)))
    usage = facts.get('recovery_usage') or {}
    if usage.get('scope') in {'last_admitted_episode', 'current_episode'}:
        label = '最近已记录调查' if usage['scope'] == 'last_admitted_episode' else '本次调查'
        rows.append(f"{label}：{usage.get('turn_count', '未知')} 次模型请求，{usage.get('tool_call_count', '未知')} 次工具调用；不是全会话累计。")
    else:
        rows.append('调查累计次数：未知（现有回执未提供完整范围）。')
    rows.append(delivery_text(facts))
    if status != 'completed':
        protection = _OUTPUT_PROTECTION.get(str(origin.get('error_code') or origin.get('reason_code') or ''))
        next_step = (protection[1] if protection else
                     '修正当前步骤证据登记的标识，再按现有流程核验与提交。' if identity_issue else
                     '先查看当前步骤的执行与核验详情，定位具体问题后再选择修复。')
        rows.append('下一步：' + next_step)
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
