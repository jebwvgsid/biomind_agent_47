"""Read-only projections of existing terminal records; no model, graph or writer ownership."""
from __future__ import annotations
from typing import Mapping
from agent_core.workflow.inspection_stop import _active_packet
from agent_core.workflow.inspection_stop import _authority
from agent_core.workflow.inspection_stop import _hash
from agent_core.workflow.inspection_stop import _mapping
from agent_core.workflow.inspection_stop import _safe
from agent_core.workflow.inspection_stop import _steps
from agent_core.workflow.inspection_stop import _terminal_packet
from copy import deepcopy
from agent_core.plan_tracker import has_materialized_plan
from agent_core.workflow.inspection_stop import inspection_stop_explanation
import json
from agent_core.plan_tracker import plan_approval_is_current
import re


PRODUCER = "managed_run_stop_v1"

def _planning_scope(durable):
    task = _mapping(durable.get("task_state"))
    failure = _mapping(task.get("planning_failure"))
    current = _mapping(durable.get("current_task"))
    return {"task_id": str(durable.get("task_id") or ""), "plan_id": "", "plan_version": "",
        "step_id": "", "attempt_id": "",
        "task_revision": int(current.get("task_revision") or durable.get("task_revision") or 0),
        "planning_epoch_id": str(_mapping(task.get("planning_epoch")).get("planning_epoch_id") or ""),
        "failure_fingerprint": str(failure.get("failure_fingerprint") or ""),
        "candidate_context_ref": str(failure.get("context_ref") or "")}

def _planning_policy(integrity):
    integrity = _mapping(integrity)
    return _mapping(integrity.get("resolved_output_policy") or integrity.get("output_policy"))

def _planning_response_explanation(label, integrity):
    integrity = _mapping(integrity)
    if not integrity:
        return label + " provider 结束原因与输出策略：UNKNOWN（未记录，不从正文或 token 数推断）。"
    finish = str(integrity.get("finish_reason") or "UNKNOWN")
    text = label + " provider：call=" + str(integrity.get("call_id") or "UNKNOWN")
    text += "；finish_reason=" + finish + "；content_status=" + str(integrity.get("content_status") or "UNKNOWN")
    if finish == "length":
        text += "；输出触及 provider 长度边界，本次候选未接纳"
    usage = integrity.get("reported_usage")
    text += "；用量记录=" + (_safe(json.dumps(usage, ensure_ascii=False), 600) if usage else "UNKNOWN")
    text += "；用量来源=" + str(integrity.get("usage_source") or integrity.get("usage_status") or "UNKNOWN") + "。"
    policy = _planning_policy(integrity)
    requested_limit = policy.get("requested_output_limit")
    text += "\n本地请求输出上限=" + str(requested_limit if requested_limit is not None else "UNKNOWN")
    text += "；wire 观察=" + str(policy.get("wire_observation_status") or "UNKNOWN")
    if "wire_output_parameter_key" in policy or "wire_output_parameter_value" in policy:
        wire_value = policy.get("wire_output_parameter_value")
        text += "；请求参数 " + str(policy.get("wire_output_parameter_key") or "UNKNOWN") + "=" + str(wire_value if wire_value is not None else "UNKNOWN")
    effective_limit = policy.get("server_effective_output_limit")
    text += "；服务端实际生效上限=" + str(effective_limit if effective_limit is not None else "UNKNOWN") + "（不把本地设置或用量当作服务端确认）。"
    parser = _mapping(integrity.get("parser_diagnostic"))
    if parser:
        text += "\n解析诊断（仅已记录字段）：" + _safe(json.dumps(parser, ensure_ascii=False), 1600)
    return text

def _scope(durable, prior_scope=None):
    task = _mapping(durable.get("task_state"))
    ref, record, packet = _active_packet(durable)
    if not packet and prior_scope:
        ref, record, packet = _terminal_packet(durable, prior_scope)
    if packet and str(packet.get("step_id") or "") != str(durable.get("current_step_id") or ""):
        ref, record, packet = "", {}, {}
    current = next((row for row in _steps(durable) if row.get("step_id") == durable.get("current_step_id")), {})
    return {"task_id": str(durable.get("task_id") or ""), "plan_id": str(durable.get("plan_id") or ""),
        "plan_version": str(durable.get("plan_version") or ""), "step_id": str(durable.get("current_step_id") or ""),
        "attempt_id": str(packet.get("attempt_ref") or current.get("verification_attempt_id") or ""),
        "episode_id": str(record.get("source_fingerprint") or _mapping(task.get("runtime_recovery")).get("active_episode_id") or "")}

def _candidate_stop_identity(durable):
    """Identity of current generation facts, independent of execution identity.

    Uses existing durable records; no epoch or counter grants another request.
    The old verification attempt is retained and compared as a causal scope.
    """
    task = _mapping(durable.get("task_state"))
    ledger = _mapping(task.get("candidate_repair_state"))
    step_id = str(durable.get("current_step_id") or "")
    return _hash({
        "generation": task.get("generate_context_episode"),
        "recovery": _mapping(task.get("recovery_state")).get("generation_claim"),
        "rejections": {key: row for key, row in _mapping(ledger.get("events")).items()
            if _mapping(row.get("scope")).get("step_id") == step_id},
        "provider_claims": {key: row for key, row in _mapping(ledger.get("provider_claims")).items()
            if _mapping(row.get("scope")).get("step_id") == step_id},
        "actions": {key: row for key, row in _mapping(_mapping(durable.get("provenance_state")).get("actions")).items()
            if row.get("step_id") == step_id},
    })

def _matches(durable, stop):
    if _mapping(durable.get("task_state")).get("status") in {"completed", "cancelled"}:
        return False
    if stop.get("planning"):
        scope = _planning_scope(durable)
        task = _mapping(durable.get("task_state"))
        failure = _mapping(task.get("planning_failure"))
        failure_current = (not failure and stop["planning"].get("source_failure_status") == "not_persisted") or (
            failure.get("status") in {stop["planning"].get("source_failure_status"), "terminal"}
            and str(failure.get("planning_epoch_id") or "") == scope["planning_epoch_id"]
            and int(failure.get("task_spec_revision") or 0) == scope["task_revision"])
        return bool(scope == stop.get("scope") and failure_current
            and not plan_approval_is_current(durable, scope["task_revision"])
            and has_materialized_plan(durable) == stop["planning"].get("plan_materialized")
            and _authority(durable) == stop.get("authority"))
    if _mapping(stop.get("boundary")).get("node") == "consultant":
        invocation = _mapping(_mapping(durable.get("task_state")).get("consultant_invocation"))
        if str(stop.get("consultant_invocation_id") or "") != str(invocation.get("invocation_id") or ""):
            return False
    actual = _scope(durable, stop.get("scope"))
    candidate_identity = stop.get("candidate_scope_identity")
    if candidate_identity:
        # Null means this candidate did not execute, never "any attempt".
        if (candidate_identity != _candidate_stop_identity(durable)
                or actual != stop.get("causal_scope")
                or stop.get("scope", {}).get("attempt_id") != ""
                or stop.get("scope", {}).get("episode_id") != ""):
            return False
        if actual["attempt_id"]:
            causal = _mapping(_mapping(_mapping(durable.get("provenance_state")).get("attempts")).get(actual["attempt_id"]))
            if not causal or any(str(causal.get(key) or "") != actual[key]
                                 for key in ("plan_id", "plan_version", "step_id")):
                return False
        observed = _mapping(_mapping(stop.get("boundary")).get("outcome")).get("scope") or {}
        if any(str(observed[key]) != str(actual.get(key, ""))
               for key in ("task_id", "plan_id", "plan_version", "step_id") if observed.get(key)):
            return False
        actual.update(attempt_id="", episode_id="")
    # A newly CREATED/STARTED attempt supersedes an old failure packet even
    # before another causal packet has been published.
    from agent_core.plan_tracker import active_attempt
    active = active_attempt(deepcopy(durable), actual["step_id"])
    if active and str(active.get("attempt_id") or "") != str(stop.get("scope", {}).get("attempt_id") or ""):
        return False
    return actual == stop.get("scope") and _authority(durable) == stop.get("authority")

def current_managed_stop(durable):
    stop = _mapping(_mapping(durable.get("task_state")).get("managed_run_stop"))
    if stop.get("producer") == PRODUCER and stop.get("persistence_status") == "committed" and _matches(durable, stop):
        return _project_current_stop_facts(durable, deepcopy(stop))
    return {}

def _project_current_stop_facts(durable, stop, observation_batches=()):
    """Read existing exact receipts; retain historical failures without replay."""
    task, scope = _mapping(durable.get("task_state")), _mapping(stop.get("scope"))
    batches = list(observation_batches)
    for receipt in _mapping(task.get("typed_recovery_receipts")).values():
        if not scope.get("episode_id") or receipt.get("source_fingerprint") != scope["episode_id"]:
            continue
        batches.append(receipt.get("causal_observation_receipts") or
                       _mapping(receipt.get("business_resume_state")).get("observation_receipts") or [])
    # Existing stop projections already froze these exact later-read receipts.
    # A later summary pass need not have the transient episode payload again.
    resolved = {(row.get("tool_call_id"), row.get("error_code")): deepcopy(row["resolved_by"])
                for row in stop.get("resolved_inspection_failures") or [] if isinstance(row.get("resolved_by"), Mapping)}
    for batch in batches:
        for index, row in enumerate(batch):
            if not isinstance(row, Mapping) or not row.get("error"):
                continue
            reference = str(row.get("source_ref") or "")
            # Mutable path/name equality cannot establish evidence identity.
            if not re.fullmatch(r"[a-zA-Z_.-]+:sha256:[0-9a-f]{64}", reference):
                continue
            for success in batch[index + 1:]:
                if (isinstance(success, Mapping) and success.get("source_ref") == reference
                        and not success.get("error") and success.get("content_sha256")):
                    resolved[(row.get("request_id"), _mapping(row.get("error")).get("code"))] = {
                        "request_id": success.get("request_id"), "tool": success.get("tool"),
                        "source_ref": reference, "content_sha256": success["content_sha256"]}
                    break
    current, history = [], []
    for failure in stop.get("inspection_failures") or []:
        identity = (failure.get("tool_call_id"), failure.get("error_code"))
        if identity in resolved:
            history.append({**deepcopy(failure), "resolution_status": "resolved", "resolved_by": resolved[identity]})
        else:
            current.append(deepcopy(failure))
    stop["current_inspection_failures"], stop["resolved_inspection_failures"] = current, history
    diagnostics = {}
    execution = _mapping(stop.get("execution"))
    # This is a recorded script report, not proof about every installed env.
    for line in str(execution.get("stdout") or "").splitlines():
        prefix = "BIOMIND_EFFECT_EVIDENCE_JSON="
        if not line.startswith(prefix):
            continue
        try:
            emitted = json.loads(line[len(prefix):])
        except (TypeError, ValueError):
            continue
        for effect in _mapping(emitted).get("explicit_side_effects") or []:
            missing = effect.get("missing_commands") if isinstance(effect, Mapping) else None
            if isinstance(missing, list) and missing and all(isinstance(item, str) for item in missing):
                diagnostics.update(missing_commands=list(dict.fromkeys(missing)),
                    source="recorded_execution_stdout", authority="script_report_only",
                    failure_stage=_safe(effect.get("identity"), 180), attempt_id=scope.get("attempt_id"))
    provenance = _mapping(durable.get("provenance_state"))
    attempt = _mapping(_mapping(provenance.get("attempts")).get(scope.get("attempt_id")))
    action = _mapping(_mapping(provenance.get("actions")).get(attempt.get("action_id")))
    if action and all(str(action.get(key) or "") == str(scope.get(key) or "") for key in ("plan_id", "plan_version", "step_id")):
        normalized = _mapping(action.get("normalized_action"))
        manifest = normalized.get("runtime_binding_manifest")
        if isinstance(manifest, Mapping) and isinstance(manifest.get("bindings"), list):
            diagnostics["runtime_binding_count"] = len(manifest.get("bindings") or [])
            diagnostics["runtime_manifest_hash"] = normalized.get("runtime_binding_manifest_hash")
            diagnostics["runtime_manifest_source"] = "exact_attempt_action"
    stop["runtime_diagnostics"] = diagnostics
    return stop

def report_delivery_status(receipts):
    """Writer lifecycle is independent from fulfilled file contracts."""
    rows = [dict((receipts or {}).get(name) or {}) for name in ("report.md", "stage_status.tsv", "reproduce.sh")]
    if all(row.get("status") in (None, "", "not_attempted") for row in rows):
        return "not_attempted"
    delivered = sum(row.get("status") in {"created", "reused", "unchanged"} and bool(row.get("sha256")) for row in rows)
    return "complete" if delivered == 3 else "partial" if delivered else "failed"

def explanation(stop):
    if stop.get("planning"):
        return _planning_explanation(stop)
    base = inspection_stop_explanation(stop)
    origin = _mapping(stop.get("origin"))
    causal = _mapping(stop.get("causal_execution"))
    reason = _mapping(stop.get("stop_reason"))
    latest = _mapping(stop.get("latest_failure"))
    candidate = _mapping(stop.get("current_candidate"))
    chain = []
    provider = _mapping(stop.get('provider_cause'))
    if provider:
        error = _mapping(provider.get('error'))
        chain.append('后续模型请求失败：服务=' + str(provider.get('service') or 'NOT_RECORDED')
            + '；模型=' + str(provider.get('model') or 'UNKNOWN')
            + '；HTTP=' + str(provider.get('http_status_code'))
            + '；type/code=' + str(error.get('type')) + '/' + str(error.get('code'))
            + '；retryable=' + str(provider.get('retryable')) + '；' + _safe(error.get('message'), 2000))
        chain.append('请求身份：logical=' + str(provider.get('semantic_invocation_id'))
            + '；attempt=' + str(provider.get('provider_attempt_id'))
            + '；诊断=' + str(provider.get('snapshot_ref') or provider.get('raw_stream_ref') or 'NOT_RECORDED'))
        previous = _mapping(stop.get('provider_previous_execution'))
        if previous:
            chain.append('已记录终态命令：attempt=' + str(previous.get('attempt_id'))
                + '；rc=' + str(previous.get('returncode')) + '；receipt=' + str(previous.get('receipt_ref'))
                + '。命令回执与步骤核验/Commit分别记账，模型请求失败不撤销该回执。')
    original_rc = causal.get("returncode", origin.get("returncode"))
    if original_rc is not None:
        chain.append("原始命令返回码：" + str(original_rc) + "。")
    current_code = latest.get("error_code") or latest.get("code") or candidate.get("reason_code")
    if current_code:
        chain.append("当前修复障碍：" + str(current_code) + "（不代表又执行了一次原命令）。")
    stop_code = str(reason.get("error_code") or "")
    if stop_code:
        chain.append("自动停止原因：" + stop_code + "。")
    if stop.get("status") == "cancelled":
        chain.append("任务取消已记录；取消来源以对应回执为准。")
    if chain:
        base = "\n".join(chain) + "\n\n" + base
    if causal.get("executed") and not _mapping(stop.get("execution")).get("executed"):
        base = base.replace("没有可确认的命令执行记录。", "末次候选尚未执行；本轮已有真实因果执行记录。")
    base = base.replace("Supervisor 待核实的疑虑：", "原始起因（断言待核实）：").replace("仍需完成语义复核", "原因记录不完整")
    base += "\n\n文件交付：" + report_delivery_status(stop.get("report_receipts")) + "（截至本次说明生成时；冻结正文不回写，最终交付以持久 receipt 为准）；" + "；".join(
        name + "=" + str((stop.get("report_receipts", {}).get(name) or {}).get("status", "not_attempted"))
        + " " + str((stop.get("report_receipts", {}).get(name) or {}).get("reason", ""))
        for name in ("report.md", "stage_status.tsv", "reproduce.sh"))
    parts = [base, "来源阶段：" + str(origin.get("source_stage") or "unknown") + "；原始错误：" + str(origin.get("error_code") or "unknown") + "；停止阶段：" + str(stop.get("stage") or "outer") + "。"]
    execution = _mapping(stop.get("execution"))
    if causal.get("attempt_id"):
        parts.append("最近因果执行：attempt=" + str(causal["attempt_id"]) + "；rc=" + str(causal.get("returncode"))
            + "；步骤=" + str(causal.get("step_id")) + "。已消费的证据只用于说明，未重新授权执行。")
        if causal.get("stderr"):parts.append("因果执行 stderr：" + _safe(causal["stderr"],2000))
        if causal.get("stdout"):parts.append("因果执行 stdout（含内部进程报告；外层 rc0 不代表步骤完成）：" + _safe(causal["stdout"],2000))
    if stop.get("current_candidate"):
        parts.append("末次候选：" + _safe(json.dumps(stop["current_candidate"],ensure_ascii=False),2000))
    if stop.get("inspection_count_scope"):
        parts.append("检查计数范围：" + json.dumps(stop["inspection_count_scope"],ensure_ascii=False))
    if stop.get("resolved_inspection_failures"):
        parts.append("历史读取失败中已有 " + str(len(stop["resolved_inspection_failures"]))
                     + " 项由随后同一证据身份的成功读取解决；原始错误和成功回执均保留，当前未解决读取问题="
                     + str(len(stop.get("current_inspection_failures") or [])) + "。")
    for attempt in stop.get("associated_attempts") or []:
        parts.append(("历史诊断 attempt：" if attempt["use"] == "historical_diagnostic" else "当前故障 attempt：")
            + attempt["attempt_id"] + "；rc=" + str(attempt["returncode"])
            + "；正式排放数=" + str(attempt["emission_count"])
            + "；观测记录数=" + str(attempt["observation_count"]) + "；" + attempt["completion"])
        parts.append("候选有效性=" + str(attempt["candidate_validity"]) + "；进程成功=" + str(attempt["process_succeeded"])
            + "；正式证据满足=" + str(attempt["formal_evidence_satisfied"]) + "；步骤完成=" + str(attempt["step_completed"]))
    if str(execution.get("purpose") or "").upper() == "DIAGNOSTIC":
        parts.append("最近 attempt 为诊断执行；其返回码不代表主步骤完成。原始执行返回码：" + str(origin.get("returncode")) + "。")
    for key in ("stdout", "stderr"):
        if execution.get(key): parts.append(key + "（脚本输出，未经独立复核）：" + _safe(execution[key], 2000))
    if stop.get("latest_failure"):
        latest = stop["latest_failure"]
        parts.append("末次候选错误：" + str(latest.get("error_code") or "unknown") + "；" + _safe(latest.get("message")))
    if stop.get("prior_completed_work"):
        parts.append("已有正式提交：" + "；".join(row["step_id"] + " (" + row["receipt"] + ")" for row in stop["prior_completed_work"]))
    if _mapping(stop.get("stop_reason")).get("termination_reason"):
        parts.append("停止策略：" + stop["stop_reason"]["termination_reason"] + "；修正记录：" + json.dumps(stop["stop_reason"].get("budget_receipt", {}), ensure_ascii=False))
    if stop.get("runtime"):
        parts.append("Runtime 事实：" + _safe(json.dumps(stop["runtime"], ensure_ascii=False), 4000))
    if stop.get("runtime_diagnostics"):
        parts.append("该次执行的运行诊断（脚本报告不证明所有环境均缺能力）："
                     + _safe(json.dumps(stop["runtime_diagnostics"], ensure_ascii=False), 1600))
    if stop.get("summary_text"):
        parts.append("AI 辅助说明（不改变上述事实）：\n" + stop["summary_text"])
    if stop.get("internal_diagnostic_delivery"):
        parts.append("内部诊断归档：" + _safe(json.dumps(stop["internal_diagnostic_delivery"],ensure_ascii=False),3000)
            + "。内部归档不等于指定文件交付成功。")
    parts.append("任务状态：" + str(stop.get("status") or "unknown").upper() + "；自动执行已停止。")
    parts.append("停止总结生成状态：" + str(stop.get("summary_status") or "IMPLEMENTED") + "（仅表示总结处理状态，不代表任务成功）" + ("；" + stop["summary_skip_reason"] if stop.get("summary_skip_reason") else ""))
    return "\n\n".join(parts)

def _planning_explanation(stop):
    facts = _mapping(stop.get("planning"))
    scope = _mapping(stop.get("scope"))
    primary = _mapping(stop.get("primary_cause"))
    latest = _mapping(stop.get("latest_failure"))
    reason = _mapping(stop.get("stop_reason"))
    parts = ["规划已停止，尚未形成批准 Plan；本轮规划没有执行、验证或提交科学步骤。",
        "原任务：" + (_safe(stop.get("task_summary"), 1800) or "当前正式 TaskSpec") + "。",
        "停止原因：" + str(reason.get("error_code") or "UNKNOWN") + "。",
        "最初拒绝：" + str(primary.get("error_code") or "PLANNER_ORIGIN_NOT_PERSISTED") + "；" + _safe(primary.get("message")),
        "末次拒绝：" + str(latest.get("error_code") or "UNKNOWN") + "；" + _safe(latest.get("message")),
        "本轮已发起 Planner 请求：" + str(reason.get("model_turns") if reason.get("model_turns") is not None else "未知") + " / 上限 " + str(_mapping(reason.get("budget_receipt")).get("max_planner_calls_per_epoch", "未知")) + "；当前连续结构无进展记录：" + str(reason.get("repeat_count", 0)) + "。",
        "任务 revision=" + str(scope.get("task_revision", "unknown")) + "；planning epoch=" + str(scope.get("planning_epoch_id") or "unknown") + "。"]
    issues = facts.get("validation_issues") or []
    parts.append(_planning_response_explanation("最初", primary.get("response_integrity")))
    parts.append(_planning_response_explanation("末次", latest.get("response_integrity")))
    first_truncation = _mapping(stop.get("first_truncation"))
    if first_truncation and first_truncation != _mapping(primary.get("response_integrity")):
        parts.append(_planning_response_explanation("首个截断", first_truncation))
    remediation = stop.get("attempted_remediation")
    if remediation:
        parts.append("实际补救记录（策略及结果以已记录状态为准）：" + _safe(json.dumps(remediation, ensure_ascii=False), 2200))
    else:
        parts.append("没有已记录的输出长度补救；不能把未执行的补救描述为已尝试。")
    if issues:
        parts.append("末次完整问题清单（详细 actual/expected/来源保留在结构化停止事实中）：\n" + "\n".join(
            "- " + str(i.get("code") or "UNKNOWN") + " / " + str(i.get("violation_code") or i.get("code") or "UNKNOWN")
            + " @ " + str(i.get("field_path") or "$")
            + (" [" + str(i.get("canonical_resource_id")) + "]" if i.get("canonical_resource_id") else "")
            + ("；诊断=" + _safe(json.dumps({key: i[key] for key in (
                "expected_type", "received_type", "position", "line", "column", "diagnostic_source", "position_source", "validator_id"
            ) if key in i}, ensure_ascii=False), 900) if any(key in i for key in ("expected_type", "received_type", "position", "line", "column", "diagnostic_source", "position_source", "validator_id")) else "")
            for i in issues if isinstance(i, Mapping)))
    else:
        parts.append("末次问题细节尚未持久记录；没有把未执行的检查推定为失败。")
    parts.append("当前没有获批步骤或执行 attempt；后续科学流程尚未由本轮规划启动。预算结束或读取失败不能证明输入不兼容、资源不存在或依赖不可用。")
    if stop.get("summary_text"):
        parts.append("AI 辅助说明（不改变上述事实）：\n" + stop["summary_text"])
    parts.append("任务状态：" + str(stop.get("status") or "unknown").upper() + "；自动执行已停止。")
    parts.append("停止总结生成状态：" + str(stop.get("summary_status") or "IMPLEMENTED") + "（仅表示总结处理状态，不代表任务成功）" + ("；" + stop["summary_skip_reason"] if stop.get("summary_skip_reason") else ""))
    if stop.get("persistence_status") != "committed":
        parts.append("停止事实尚未确认持久化；本进程不会因此继续规划。")
    if stop.get("report_paths"):
        parts.append("已生成停止报告：" + "；".join(str(v) for v in stop["report_paths"].values()))
    else:
        parts.append("尚未确认生成报告文件。")
    receipts = _mapping(stop.get("report_receipts"))
    parts.append("文件交付状态（截至说明生成时；报告自身最终写入结果以持久 receipt 为准）：" + "；".join(
        name + "=" + str(_mapping(receipts.get(name)).get("status") or "not_attempted")
        + " / " + str(_mapping(receipts.get(name)).get("reason") or ("WRITER_NOT_RUN" if name not in receipts else "receipt recorded"))
        for name in ("report.md", "stage_status.tsv", "reproduce.sh")))
    parts.append("没有科学执行 attempt；reproduce.sh 仅记录规划未接纳且未运行，不包含科学复现命令。原任务的科学交付尚未完成。")
    if stop.get("secondary_failures"):
        parts.append("附加问题：" + "；".join(str(i.get("error_code") or "UNKNOWN") for i in stop["secondary_failures"]))
    return "\n\n".join(parts)

def projection(stop):
    if not stop or stop.get("resolution") == "stale_scope": return {}
    d = {"error_code": stop["stop_reason"]["error_code"], "stage": stop.get("stage", "outer"), "error_owner": stop.get("stage", "outer").split(".")[0],
         "reason": explanation(stop), "detail": explanation(stop), "stop_id": stop["stop_id"], "origin": stop["origin"],
         "primary_cause": stop.get("primary_cause", {}), "latest_failure": stop.get("latest_failure", {}),
         "prior_completed_work": stop.get("prior_completed_work", []),
         "unresolved_findings": deepcopy(stop.get("unresolved_findings") or []),
         "stages": deepcopy(stop.get("stages") or []),
         "recovery_usage": deepcopy(stop.get("recovery_usage") or {}),
         "execution": stop["execution"], "causal_execution":stop.get("causal_execution",{}),
         "current_candidate":stop.get("current_candidate",{}), "inspection_count_scope":stop.get("inspection_count_scope",{}), "inspection_failures": stop.get("inspection_failures", []), "stop_reason": stop["stop_reason"],
         "current_inspection_failures":deepcopy(stop.get("current_inspection_failures", stop.get("inspection_failures", []))),
         "resolved_inspection_failures":deepcopy(stop.get("resolved_inspection_failures") or []),
         "runtime_diagnostics":deepcopy(stop.get("runtime_diagnostics") or {}),
         "internal_diagnostic_delivery":deepcopy(stop.get("internal_diagnostic_delivery") or {}),
         "required_output_status":stop.get("required_output_status", "unknown"),
         "scientific_completion":stop.get("scientific_completion", "not_completed"),
         "delivery_status": report_delivery_status(stop.get("report_receipts")), "report_paths": stop["report_paths"], "report_receipts": stop["report_receipts"], "persistence_status": stop["persistence_status"],
         "summary_status": stop["summary_status"], "summary_llm_calls": stop["summary_llm_calls"],
         "secondary_causes": stop["secondary_failures"]}
    if stop.get("planning"):
        d["planning"] = deepcopy(stop["planning"])
        d["validation_issues"] = deepcopy(stop["planning"].get("validation_issues") or [])
        d["scope"] = deepcopy(stop["scope"])
        for key in ("response_integrity", "first_truncation", "output_policy", "attempted_remediation"):
            d[key] = deepcopy(stop.get(key) or {})
    if stop["report_paths"].get("report.md"): d["report_path"] = stop["report_paths"]["report.md"]
    return {"status": stop["status"], "terminal_diagnostic": d}

def finish_update(decision):
    from langchain_core.messages import AIMessage
    # Process and step facts remain independent of the model's ending label.
    message = decision.get('message') or '当前请求已结束；请查看保存的执行与交付记录。'
    for row in (decision.get('delivery_receipts') or {}).values():
        if row.get('status') in {'conflict', 'failed', 'missing'}:
            message += '\n\n文件交付未完成：' + str(row.get('path')) + '；' + str(row.get('reason'))
            if row.get('internal_diagnostic'):
                message += '\n内部诊断：' + row['internal_diagnostic']
    active = [r for r in (decision.get('facts') or {}).get('attempts', []) if r.get('status') in {'RUNNING', 'STARTING', 'UNKNOWN_AFTER_CRASH'}]
    if active:
        message += '\n\n运行事实：仍有运行中或状态待核实的进程；此结束决定没有取消它们。'
    facts = decision.get('facts') or {}
    if facts.get('completion_source') == 'model_report':
        message = '模型报告（' + str(decision.get('model_outcome')) + '）：\n' + message
    unresolved = [r for r in (facts.get('causal_summary') or {}).get('runs', [])
                  if r.get('terminal') is not True or r.get('cleanup_confirmed') is not True]
    if unresolved and not active:
        message += '\n\n运行事实：仍有资源待核对；请求结束没有释放其 owner。'
    failures = [r for r in facts.get('attempts', []) if r.get('returncode') not in (None, 0)]
    if failures:
        message += '\n\n执行记录：非零退出码 ' + ', '.join(str(r['returncode']) for r in failures) + '；原始回执保留，以上模型结论不等于机器科学核验。'
    return {'messages': [AIMessage(content=message)], 'request_finish': deepcopy(decision),
        'next_step': 'finish', 'generate_exit_reason': 'request_finished'}
