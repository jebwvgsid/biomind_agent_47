"""Display projections and explicit exception archives; no business state loading."""
import asyncio
import json
import re


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
    truncation = result.get('truncated')
    truncated = any(value is True for value in truncation.values()) if isinstance(truncation, dict) else bool(truncation)
    if truncated and type(cursor.get('next_start_line')) is int:
        facts.append(f"分页；可从第 {cursor['next_start_line']} 行继续")
    elif truncated:
        logs = result.get('log_refs') or {}
        readable = [str(logs[key]) for key in ('stdout', 'stderr') if logs.get(key)
                    and (not isinstance(truncation, dict) or truncation.get(key) is True)]
        if readable:
            facts.append('工具内容有裁剪；可用 exec_command 定点分页读取日志：' + '，'.join(readable))
        elif result.get('observation_ref'):
            facts.append('工具内容有裁剪；可按 observation_ref 续读')
        else:
            facts.append('工具内容有裁剪；续读位置未提供')
    if observation.get('truncated') and not truncated:
        facts.append('模型可见工具内容已裁剪')
        if result.get('observation_ref'):
            facts.append('完整观测已归档，可按回执引用读取')
    if status == 'NO_MATCH':
        facts.append('未找到匹配项')
    if not facts:
        facts.append('已返回')
    return label + '：' + '；'.join(facts), ('partial' if observation.get('truncated') or truncated else 'empty' if status == 'NO_MATCH' else 'returned')


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
    execution = facts.get('execution') or {}
    same_attempt = (not origin.get('attempt_id') or origin.get('attempt_id') == execution.get('attempt_id'))
    if (code in {'VALIDATION_FAILED', 'OBSERVED_ARTIFACT_CONTENT_INVALID'}
            or (code == 'EXECUTION_FAILED' and same_attempt and execution.get('validation_status') == 'failed')):
        rc = origin.get('returncode', execution.get('returncode'))
        label = f'进程退出码 {rc}；' if type(rc) is int else ''
        detail = '必需产物内容未通过正式核验' if code == 'OBSERVED_ARTIFACT_CONTENT_INVALID' else '正式产物核验失败'
        return label + detail + '（' + code + '）；当前步骤尚未正式提交。', False
    if facts.get('unresolved_findings'):
        rc = execution.get('returncode')
        label = f'进程退出码 {rc}；' if type(rc) is int else ''
        return label + '当前检查未通过（' + (code or 'UNKNOWN') + '）；具体问题见下方记录，当前步骤尚未正式提交。', False
    if code == 'EXECUTION_FAILED':
        rc = origin.get('returncode')
        if rc == 0:
            return '进程退出码 0；执行后的失败原因仍待核对（EXECUTION_FAILED）。', False
        label = '脚本执行失败' + (f'，退出码 {rc}' if type(rc) is int else '')
        runtime = facts.get('runtime_diagnostics') or {}
        if runtime.get('authority') == 'script_report_only' and runtime.get('missing_commands'):
            label += '；该次脚本报告 missing_commands=[' + _brief(', '.join(runtime['missing_commands'])) + ']'
            if runtime.get('failure_stage'):
                label += '，阶段=' + _brief(runtime['failure_stage'])
            if runtime.get('runtime_manifest_source') == 'exact_attempt_action' and runtime.get('runtime_binding_count') == 0:
                label += '；该动作正式 runtime bindings 为空'
            return label + '；已有环境是否能提供所需能力仍待验证（EXECUTION_FAILED）。', False
        return label + '；具体原因尚未定位（EXECUTION_FAILED）。', False
    labels = {'DELIVERY_CONTRACT_UNMET': '交付条件尚未满足',
              'WORKSPACE_MUTATION_NOT_ATTESTED': '工作区变更未通过正式核验',
              'WORKSPACE_MUTATION_RECEIPT_INVALID': '工作区变更核验回执无效'}
    if code in labels:
        return labels[code] + '（' + code + '）。', False
    recorded = _brief(origin.get('detail') or origin.get('message') or origin.get('summary'), 360)
    if recorded:
        return recorded + ('（' + code + '）' if code else '') + '。', False
    return ('尚未定位具体原因（' + code + '）。' if code else '具体原因尚未定位。'), False


def _stop_text(stop, code):
    if code == 'PLANNING_EPOCH_EXHAUSTED':
        budget = stop.get('budget_receipt') or {}
        used = budget.get('planner_calls_total', stop.get('model_turns'))
        limit = budget.get('max_planner_calls_per_epoch')
        count = (str(used) if type(used) is int else '未知') + '/' + (str(limit) if type(limit) is int else '未知')
        last = budget.get('last_validation_codes') or []
        return ('Planner 请求额度已耗尽（已用/上限：' + count + '）；尚未形成可批准计划。'
                + ('最后阻塞：' + '、'.join(_brief(item, 120) for item in last[:6]) + '。' if last else '')
                + '（' + code + '）。')
    detail = str(stop.get('detail') or '')
    if code == 'RECOVERY_SEED_CAPACITY_EXCEEDED':
        # Historical receipts retain their original units and admission stage.
        counts = dict(re.findall(r'\b(chars|limit)=(\d+)\b', detail))
        amount = ('，' + counts['chars'] + ' / ' + counts['limit'] + ' 字符'
                  if 'chars' in counts and 'limit' in counts else '')
        return 'Recovery 固定 seed 在首次调用前超过字符限制' + amount + '（' + code + '）。'
    if ('TOKEN_LIMIT' in code or 'COMPACTION' in code or 'CONTEXT' in code
            or code == 'RECOVERY_PROVIDER_INPUT_UNSAFE'):
        budget = stop.get('budget') or {}
        # The failed dispatch's feedback takes precedence over an outer
        # maintenance policy. A source_request_budget belongs to an earlier
        # business request and must not label a failed compactor request.
        for _ in range(4):
            feedback = budget.get('capacity_feedback') if isinstance(budget, dict) else None
            if not isinstance(feedback, dict) or not feedback:
                break
            budget = feedback
        if isinstance(budget, dict) and all(type(budget.get(key)) is int
                for key in ('estimated_input_tokens', 'safe_input_tokens')):
            stage = _brief(budget.get('failure_stage') or stop.get('failure_stage') or 'recovery.context_budget')
            output = budget.get('effective_output_limit_tokens', budget.get('output_reserve_tokens'))
            output_text = '，有效输出上限 ' + str(output) + ' tokens' if type(output) is int else ''
            return (stage + ' 的完整请求输入估算为 ' + str(budget['estimated_input_tokens'])
                    + ' tokens，有效输入上限 ' + str(budget['safe_input_tokens']) + ' tokens'
                    + output_text + '；已停止（' + code + '）。')
        counts = dict(re.findall(r'\b(tokens|safe)=(\d+)\b', detail))
        if 'tokens' in counts and 'safe' in counts:
            stage = _brief(stop.get('failure_stage') or 'recovery.context_budget')
            return (stage + ' 的输入估算为 ' + counts['tokens'] + ' tokens，有效输入上限 '
                    + counts['safe'] + ' tokens；已停止（' + code + '）。')
    if code == 'RECOVERY_RESPONSE_INCOMPLETE':
        finish = str(stop.get('finish_reason') or detail)
        truncated = finish in {'length', 'max_tokens', 'max_output_tokens'}
        label = ('Recovery 单次输出达到上限，响应未完整提交' if truncated
                 else 'Recovery 响应未完整接纳，未提交恢复指令')
        used = stop.get('output_resubmission_count')
        if type(used) is int:
            label += f'；本次因果调查已完整重提 {used} 次'
        if stop.get('output_resubmission_exhausted') is True:
            label += '；额外完整重提额度已耗尽'
        elif type(stop.get('output_resubmissions_remaining')) is int:
            label += f"；剩余额外重提 {stop['output_resubmissions_remaining']} 次"
        return label + '（' + code + '）。'
    if code in {'RECOVERY_NO_PROGRESS', 'RECOVERY_NO_PROGRESS_LIMIT'}:
        rounds = stop.get('no_progress_rounds')
        prefix = f'连续 {rounds} 轮' if type(rounds) is int and rounds > 0 else '恢复调查'
        return prefix + '未取得可记录进展，达到停止条件；自动纠错已停止（' + code + '）。'
    descriptions = {
        'GENERATE_STOP_REQUESTED': '模型提交 blocked，已停止自动修复；任务未完成',
        'GENERATE_REPAIR_BUDGET_EXHAUSTED': '候选修正次数已耗尽，未再次启动被拒绝的候选',
        'GENERATE_IDENTICAL_CANDIDATE_EXHAUSTED': '相同候选重复被拒绝，已停止自动修复',
        'RECOVERY_BUDGET_EXHAUSTED': '恢复调查次数预算已耗尽，自动纠错已停止',
        'RECOVERY_RESOURCE_LIMIT': '恢复调查累计资源达到上限，自动纠错已停止',
        'RECOVERY_PROVIDER_TRANSPORT_FAILED': '恢复请求传输失败，已结束本次自动调查',
    }
    return descriptions.get(code, '已按记录的停止条件结束；具体触发细节见详情') + '（' + code + '）。'


def terminal_text(status, facts):
    if status == 'request_ended' and facts.get('request_finish'):
        from agent_core.terminal_facts import finish_update
        return '本次请求已结束\n' + finish_update(facts['request_finish'])['messages'][0].content

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
        provider = facts.get('provider_cause') or stop.get('provider_cause') or {}
        if provider:
            error = provider.get('error') or {}
            rows.append('模型请求失败：' + _brief(provider.get('service') or '服务未记录')
                + ' · ' + _brief(provider.get('model') or '模型未记录')
                + ' · HTTP ' + str(provider.get('http_status_code'))
                + ' · ' + _brief(error.get('code') or error.get('type'))
                + '；可重试=' + str(provider.get('retryable')) + '。')
            rows.append('供应商原因：' + _brief(error.get('message'), 1200))
            previous = facts.get('provider_previous_execution') or {}
            if previous:
                rows.append('已有终态命令回执：' + _brief(previous.get('attempt_id'))
                    + '；rc=' + str(previous.get('returncode')) + '；' + _brief(previous.get('receipt_ref'), 240)
                    + '。回执保留；命令完成不等于步骤核验/Commit完成。')
        causal = facts.get('causal_execution') or {}
        causal_rc = causal.get('returncode', origin.get('returncode'))
        if type(causal_rc) is int:
            rows.append('原始执行返回码：' + str(causal_rc) + '。')
        latest = facts.get('latest_failure') or {}
        candidate = facts.get('current_candidate') or {}
        current_code = latest.get('error_code') or latest.get('code') or candidate.get('reason_code')
        if current_code and current_code != origin.get('error_code'):
            rows.append('当前修复障碍：' + _brief(current_code)
                        + ('；候选尚未执行。' if candidate.get('execution_started') is False else '。'))
        for finding in facts.get('unresolved_findings') or []:
            if not isinstance(finding, dict):
                continue
            label = _brief(finding.get('code') or 'UNKNOWN', 160)
            detail = _brief(finding.get('detail'), 360)
            source = _brief(finding.get('source') or finding.get('source_pointer'), 180)
            position = _brief(finding.get('field_path'), 180)
            rows.append('当前未解决检查：' + label + (' · ' + detail if detail else '')
                        + ('；来源=' + source if source else '') + ('；位置=' + position if position else '') + '。')
        candidates = execution.get('candidate_outputs') or []
        if candidates:
            count = execution.get('candidate_output_count', len(candidates))
            rows.append(f'本次候选产物：{count} 项已有物化记录；文件存在不代表核验通过或步骤提交。')
            for item in candidates[:5]:
                if isinstance(item, dict):
                    exists = ('存在' if item.get('exists') is True else '不存在' if item.get('exists') is False else '存在状态未记录')
                    checked = ('内容检查未通过' if item.get('content_valid') is False else
                               '局部内容检查通过，正式接纳见步骤状态' if item.get('content_valid') is True else '内容有效性未确认')
                    rows.append('候选：' + _brief(item.get('path'), 240) + '；' + exists + '；' + checked + '。')
    failures = list(dict.fromkeys(str(r.get('error_code') or 'UNKNOWN') for r in facts.get('current_inspection_failures', facts.get('inspection_failures', []))))
    if failures:
        labels = {'ARTIFACT_LOCATOR_MISSING': '未取得产物的可读位置',
                  'ARTIFACT_PERMISSION_DENIED': '读取产物时权限不足',
                  'FILE_NOT_FOUND': '读取时目标文件不存在'}
        rows.append('恢复取证问题：' + '；'.join(labels.get(code, '读取证据失败') + '（' + code + '）' for code in failures))
    if facts.get('resolved_inspection_failures'):
        rows.append(f"历史取证错误中 {len(facts['resolved_inspection_failures'])} 项已有后续成功读取，错误记录仍保留。")
    trigger = stop.get('error_code') or facts.get('error_code')
    if trigger:
        rows.append('停止触发：' + _stop_text(stop, str(trigger)))
    usage = facts.get('recovery_usage') or {}
    if usage.get('scope') == 'current_admission':
        rows.append('本次 Recovery 准入在派发前停止：0 次模型请求，0 次工具调用；不是全会话累计。')
    elif usage.get('scope') in {'last_admitted_episode', 'current_episode'}:
        label = '最近已记录调查' if usage['scope'] == 'last_admitted_episode' else '本次调查'
        rows.append(f"{label}：{usage.get('turn_count', '未知')} 次模型请求，{usage.get('tool_call_count', '未知')} 次工具调用；不是全会话累计。")
    elif facts.get('stage') != 'planner':
        rows.append('调查累计次数：未知（现有回执未提供完整范围）。')
    rows.append(delivery_text(facts))
    if status != 'completed':
        protection = _OUTPUT_PROTECTION.get(str(origin.get('error_code') or origin.get('reason_code') or ''))
        next_step = ('查看被拒绝的 Plan 候选、上述资源字段冲突及规划反馈，修正完整计划后重新提交；本轮尚未执行科学步骤。'
                     if facts.get('stage') == 'planner' and execution.get('executed') is False else
                     protection[1] if protection else
                     '修正当前步骤证据登记的标识，再按现有流程核验与提交。' if identity_issue else
                     '验证已声明的环境与所需命令，按正式修订和批准流程重新生成当前未完成步骤。' if (facts.get('runtime_diagnostics') or {}).get('missing_commands') else
                     '根据上述具体检查记录修复当前未完成步骤，再走现有核验与提交流程。' if facts.get('unresolved_findings') else
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
    # Only successful current publication receipts expose openable paths. A
    # failed protected target is never presented as the current report.
    paths = []
    for label, locations, publications in (
        ('本次报告', facts.get('report_paths') or {}, receipts),
        ('本次内部诊断', internal.get('paths') or {}, internal.get('receipts') or {}),
    ):
        for name in ('report.md', 'stage_status.tsv', 'reproduce.sh'):
            path, receipt = locations.get(name), publications.get(name) or {}
            if (isinstance(path, str) and path.startswith('/')
                    and receipt.get('status') in {'created', 'reused', 'unchanged'} and receipt.get('sha256')):
                paths.append(label + ' ' + name + '：' + path)
    if paths:
        text += '\n' + '\n'.join(paths)
    return text


def delivery_identity(facts):
    return json.dumps({key: facts.get(key) for key in (
        'report_receipts', 'required_output_status', 'delivery_status', 'internal_diagnostic_delivery')}, sort_keys=True, default=str)


def cancellation_receipt_text(receipt, provider_record, active_process_groups):
    """Project existing receipts, without asserting unobserved global cleanup."""
    saved = bool(receipt.get('receipt_id'))
    parts = ['任务取消已登记' if saved else '任务取消登记未确认']
    cleanup = provider_record.get('cleanup_result')
    if cleanup == 'CLEANUP_CONFIRMED':
        parts.append('末次模型请求本地清理已确认')
    elif cleanup == 'CLEANUP_NOT_REQUIRED':
        parts.append('末次模型请求无需本地清理')
    else:
        parts.append('末次模型请求本地清理未确认')
    count = len(active_process_groups)
    parts.append(f'当前仍登记 {count} 个子进程组' if count else '当前无登记中的子进程组')
    # An empty local registry is not a receipt for arbitrary remote jobs.
    parts.append('以上为本地回执，未声明远端任务已终止')
    return '；'.join(parts) + '。'


def expected_user_cancellation(error, *, task_id, request_scope,
                               cancellation_receipt, owner_state):
    """Classify only an explicitly attributed, scoped local cancellation.

    This projection neither catches exceptions nor changes runtime outcome.
    An unknown failure during cancellation remains a failure.
    """
    from collections.abc import Mapping
    if not isinstance(error, asyncio.CancelledError):
        return False
    receipt, owner = cancellation_receipt or {}, owner_state or {}
    scope = request_scope if isinstance(request_scope, Mapping) else {}
    recorded = receipt.get('scope') or {}
    if not (task_id and receipt.get('receipt_id') and receipt.get('task_id') == task_id
            and receipt.get('initiator_kind') == 'user' and receipt.get('source_evidence')
            and recorded.get('task_id') == task_id and scope.get('task_id') == task_id
            and owner.get('task_id') == task_id and owner.get('cancel_requested') is True
            and not str(receipt.get('status') or '').startswith('not_committed')):
        return False
    return all(recorded.get(key) == value for key, value in scope.items()
               if value is not None and key in {
                   'task_id', 'task_revision', 'planning_epoch_id', 'plan_id',
                   'plan_version', 'task_semantic_hash'})


def archive_runtime_exception(directory, error, *, task_id, request_scope,
                              cancellation_receipt=None, owner_state=None,
                              fallback_state=None, cleanup_refs=None, source='runtime'):
    """Write a full sanitized chain without locals to a caller-owned directory.

    The caller supplies current receipts and decides whether to re-raise. This
    helper never treats archive success as process/provider cleanup evidence.
    """
    import os
    import tempfile
    import traceback
    import uuid
    from datetime import datetime, timezone
    from pathlib import Path
    from agent_core.cli_display import safe_public_text

    expected = expected_user_cancellation(error, task_id=task_id,
        request_scope=request_scope, cancellation_receipt=cancellation_receipt,
        owner_state=owner_state)
    # capture_locals=False also avoids leaking environment/configuration values.
    full_traceback = ''.join(traceback.TracebackException.from_exception(
        error, capture_locals=False).format(chain=True))
    record = {
        'schema_version': 1, 'task_id': task_id, 'source': source,
        'recorded_at': datetime.now(timezone.utc).isoformat(),
        'exception_type': type(error).__module__ + '.' + type(error).__qualname__,
        'expected_user_cancellation': expected,
        'request_scope': request_scope,
        'cancellation_receipt': cancellation_receipt or {},
        'owner_state': owner_state or {}, 'fallback_state': fallback_state or {},
        'cleanup_refs': cleanup_refs or {},
        'traceback': safe_public_text(full_traceback),
    }
    temporary = None
    try:
        target_dir = Path(directory)
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / ('exception-' + uuid.uuid4().hex + '.json')
        # Sanitize values before serialization: redacting serialized JSON can
        # remove its quotes and make an otherwise complete archive unreadable.
        def sanitized(value):
            if isinstance(value, dict):
                return {str(key): ('[REDACTED]' if re.fullmatch(
                    r'(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret)', str(key))
                    else sanitized(item)) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [sanitized(item) for item in value]
            if isinstance(value, (str, bytes)):
                return safe_public_text(value)
            if value is None or isinstance(value, (bool, int, float)):
                return value
            return safe_public_text(str(value))
        wire = json.dumps(sanitized(record), ensure_ascii=False, indent=2)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target_dir,
                                         prefix='.exception-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(wire + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        return {'status': 'saved', 'expected_user_cancellation': expected,
                'diagnostic_ref': str(destination),
                'display_text': ('已按用户请求取消；清理状态见回执。' if expected else
                                 '运行发生异常；完整诊断已归档。') + ' 诊断：' + str(destination)}
    except Exception as archive_error:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return {'status': 'failed', 'expected_user_cancellation': expected,
                'diagnostic_ref': '', 'archive_error_type': type(archive_error).__name__,
                'archive_error': safe_public_text(str(archive_error)),
                'display_text': '异常诊断归档失败；不能确认已保存完整异常。'}


def task_request_projection(data_path, task_id):
    """Read one displayed task only, without migration, model, DB or state writes."""
    import json
    import re
    from pathlib import Path
    from agent_core.request_state import current_finish
    unknown = {'request': 'UNKNOWN', 'plan': 'UNKNOWN'}
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}', task_id):
        return unknown
    path = Path(data_path) / 'task_outputs' / task_id / 'plan_state.json'
    try:
        with path.open('r', encoding='utf-8') as stream:
            state = json.load(stream)
        if not isinstance(state, dict) or state.get('task_id') != task_id:
            return unknown
        finish = current_finish(state)
        task = state.get('task_state') or {}
        if task.get('execution_architecture') == 'llm_tools_v4':
            notes = task.get('work_plan') or {}
            todos = notes.get('todos') or []
            active = [r for r in (state.get('provenance_state') or {}).get('attempts', {}).values()
                      if r.get('status') in {'STARTING','RUNNING','UNKNOWN_AFTER_CRASH'}]
            return {'request': str(finish['status'])+'/'+str(finish.get('model_outcome','UNKNOWN')) if finish else str(task.get('status') or 'UNKNOWN'),
                    'plan': 'model_report' if notes else 'optional',
                    'completed_todos': sum(r.get('status') == 'completed' for r in todos),
                    'total_todos': len(todos), 'unsettled_resources': len({str(r.get('attempt_id') or r.get('run_ref')) for r in active} | {str(r.get('run_ref') or r.get('attempt_id')) for r in (finish.get('facts') or {}).get('causal_summary', {}).get('runs', []) if r.get('terminal') is not True})}
        steps = state.get('steps') or []
        plan = ('completed' if all(r.get('status') == 'done' for r in steps) else 'incomplete') if steps else 'UNKNOWN'
        request = (str(finish['status']) + '/' + str(finish.get('model_outcome', 'UNKNOWN'))
                   if finish else str((state.get('task_state') or {}).get('status') or 'UNKNOWN'))
        done = {row.get('step_id') for row in steps if row.get('status') == 'done'}
        receipts = (state.get('task_state') or {}).get('step_commit_receipts') or {}
        committed = {row.get('step_id') for row in receipts.values() if isinstance(row,dict) and row.get('step_id') in done}
        return {'request': request, 'plan': plan, 'committed_steps': len(committed), 'total_steps': len(steps)}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return unknown
