"""Common presentation owner for already terminal schema-2 managed runs.

Called once by the outer run boundary. Graph routes and business stop decisions
remain authoritative. Base facts exist before any storage or provider operation.
"""
from __future__ import annotations
import asyncio
import json
import re
import time
from copy import deepcopy
from pathlib import Path
from typing import Mapping
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END
from agent_core.plan_tracker import (load_plan_state, save_plan_state, set_task_status,
    canonical_task_spec_for_state, plan_approval_is_current, has_materialized_plan)
from agent_core.workflow.inspection_stop import (
    _mapping, _safe, _code, _hash, _authority, _active_packet, _steps,
    _report_targets, _internal_report_targets, _report_bodies, _write_report, _secondary, _terminal_packet,
    build_inspection_stop, inspection_stop_explanation,
)
from agent_core.workflow.runtime_recovery_reporting import _path, publish_report

PRODUCER = "managed_run_stop_v1"
SUMMARY_PROMPT = """优先返回 {"fact_ids": [冻结事实键]}，选择最能解释停止的事实。旧 explanation 为未验证表达，不作为正式事实采用。
自动执行和恢复已停止。你只能解释冻结事实，不能继续解决任务。
分别说明原始失败、恢复障碍、停止条件、实际进度与未完成项。脚本的断言未经独立验证时不能作为客观数据结论。
读取失败不证明文件不存在，预算用尽不证明依赖不存在。不得声称有未发生的查询、修复、重跑、验证或文件。
不能调用工具、给出执行 directive、改变状态、要求内部 handle、重新规划或开始 Recovery。
只返回 JSON {\"fact_ids\":[\"origin\",\"stop_reason\"]}。兼容旧 explanation 字段，但自由文字只归档，不作为正式事实采用。
解释是非权威辅助文字；程序事实面板始终保留。证据不足就说明尚未确认。"""


def observe_managed_node(name, handler):
    """Record the last completed boundary, never infer an END from old fields."""
    async def observed(state):
        result = dict(await handler(state))
        field = "verification_outcome" if name == "supervisor" else name + "_outcome"
        result["managed_run_boundary"] = {"node": name, "field": field,
            "outcome": deepcopy(result.get(field) or {}),
            "scope": {key: state.get(key, "") for key in ("task_id", "plan_id", "plan_version", "current_step_id")}}
        return result
    return observed


def terminal_boundary(values, *, exception=None):
    """Consume only validated outcomes of the last finished production node."""
    from agent_core.workflow import outcomes as contracts
    from agent_core.workflow import runtime_graph as routes
    boundary = _mapping(values.get("managed_run_boundary"))
    if exception is not None:
        return {"node": "outer", "status": "failed", "outcome": {"type": "caught_exception", "summary": type(exception).__name__ + ": " + _safe(exception),
            "payload": {"error_code": "MANAGED_RUN_INTERNAL_EXCEPTION"}}}
    if not boundary:
        return {}
    node, raw = boundary.get("node"), _mapping(boundary.get("outcome"))
    if not raw:
        return {}  # direct answer or a subgraph boundary without a terminal fact
    names = {"generate": "GenerateOutcome", "execute": "ExecuteOutcome", "supervisor": "VerificationOutcome",
        "commit_step": "CommitStepOutcome", "recovery": "RecoveryOutcome", "final_audit": "FinalAuditOutcome",
        "triage": "TriageOutcome", "consultant": "ConsultantOutcome", "planner": "PlannerOutcome",
        "plan_review": "PlanReviewOutcome", "ask_user": "AskUserOutcome", "install_subagent": "InstallSubagentOutcome"}
    if node == "step_worker":
        from agent_core.workflow.step_worker import StepOutcome
        contract = StepOutcome
    else:
        contract = getattr(contracts, names.get(node, ""), None)
    if contract is None:
        return {}
    outcome = contract.from_mapping(raw).to_dict()
    kind, payload = outcome["type"], _mapping(outcome.get("payload"))
    if payload.get("resolution_disposition") in {"needs_user_decision", "evidence_ready", "stale_scope"}:
        return {}
    if kind in {"cancelled", "approval_pending", "lightweight_completed", "answer_ready"}:
        return {}
    route_name = {"supervisor":"verification", "install_subagent":"install"}.get(node,node)
    router = getattr(routes, "route_" + route_name + "_outcome", None)
    if router is None or router({boundary["field"]: raw}) != END:
        return {}
    if node == "ask_user" and kind in {"answer_recorded", "question_missing"}:
        return {}  # Unknown semantic continuation is not a failure decision.
    if node == "triage" and kind == "resume_classified" and values.get("task_status") not in {"failed", "blocked"}:
        return {}
    # 'terminal' and FinalAudit represent both successes and failures.
    explicit = str(payload.get("status") or payload.get("terminal_status") or payload.get("task_status") or "")
    if explicit in {"completed", "success", "cancelled", "waiting_user", "paused_retryable"}:
        return {}
    if node == "final_audit" and kind not in {"task_incomplete", "entry_rejected", "harness_error"}:
        return {}
    if kind == "terminal" and explicit not in {"failed", "blocked", "error"}:
        return {}
    status = explicit if kind == "terminal" and explicit in {"blocked", "failed"} else "blocked" if kind in {"blocked", "runtime_recovery_stopped"} else "failed"
    return {"node": node, "status": status, "outcome": outcome}


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


def _planning_issue_summary(issues):
    first = _mapping(next(iter(issues or []), {}))
    return {"error_code": str(first.get("code") or "PLANNER_ORIGIN_NOT_PERSISTED"),
        "message": _safe(first.get("message") or first.get("reason") or ""),
        "validation_issues": deepcopy(list(issues or []))}


def _planning_policy(integrity):
    integrity = _mapping(integrity)
    return _mapping(integrity.get("resolved_output_policy") or integrity.get("output_policy"))


def _planning_response_projection(integrity):
    """Small explanation facts; the complete scoped receipt stays in planning."""
    integrity = _mapping(integrity)
    return {key: deepcopy(integrity[key]) for key in (
        "call_id", "provider_attempt_id", "provider_request_id", "finish_reason",
        "reported_usage", "usage_source", "usage_status", "transport_status", "content_status", "parser_diagnostic",
    ) if key in integrity}


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


def _build_planning_stop(task_id, durable, boundary, planning):
    """Build the entire no-Plan base from typed, already held facts."""
    planning = deepcopy(planning)
    scope = deepcopy(_mapping(planning.get("scope")))
    terminal = _mapping(planning.get("terminal_diagnostic"))
    payload = _mapping(boundary["outcome"].get("payload"))
    code = str(terminal.get("error_code") or payload.get("error_code") or payload.get("reason_code") or "PLANNER_TERMINAL_REASON_NOT_PERSISTED")
    primary = _mapping(planning.get("primary_failure"))
    primary_summary = _planning_issue_summary(primary.get("validation_issues"))
    primary_summary.update(status=primary.get("status", "not_persisted"), source=deepcopy(primary))
    primary_summary["response_integrity"] = deepcopy(_mapping(primary.get("response_integrity")))
    latest = _planning_issue_summary(planning.get("validation_issues"))
    latest["candidate_source"] = deepcopy(planning.get("candidate_source") or {})
    latest["response_integrity"] = deepcopy(_mapping(planning.get("response_integrity")))
    remediation = deepcopy(planning.get("attempted_remediation") or latest["response_integrity"].get("attempted_remediation") or [])
    epoch = _mapping(planning.get("planning_epoch"))
    value = {"schema_version": 1, "producer": PRODUCER, "status": boundary["status"],
        "scope": scope, "authority": _authority(durable), "boundary": deepcopy(boundary),
        "planning": planning, "primary_cause": primary_summary, "latest_failure": latest,
        "response_integrity": deepcopy(latest["response_integrity"]),
        "first_truncation": deepcopy(_mapping(planning.get("first_truncation"))),
        "output_policy": deepcopy(_planning_policy(latest["response_integrity"]) or _mapping(planning.get("output_policy"))),
        "attempted_remediation": remediation,
        "origin": {"source_stage": "planner", "source_owner": "planner", "failure_type": "planning_candidate_rejected",
            "error_code": primary_summary["error_code"], "summary": primary_summary["message"],
            "evidence_refs": [str(planning["primary_context_ref"])] if planning.get("primary_context_ref") else []},
        "task_summary": _safe(planning.get("task_summary"), 1800),
        "stage": "planner", "stop_reason": {"error_code": code, "detail": _safe(terminal.get("detail") or boundary["outcome"].get("summary")),
            "budget_receipt": deepcopy(terminal.get("machine_evidence") or {}),
            "model_turns": int(epoch.get("planner_calls_total") or 0), "repeat_count": int(planning.get("repeat_count") or 0)},
        "execution": {"step_id": "", "attempt_id": "", "executed": False, "returncode": None,
            "deterministic_passed": False, "validation_status": "not_performed", "reviewer_disposition": "not_performed",
            "committed": False, "step_status": "not_started", "command_body_available": False},
        "stages": [], "prior_completed_work": [], "inspection_failures": [], "publication_failures": [],
        "resolution": "unresolved", "automatic_actions_stopped": True, "technical_classification": "planning_unresolved",
        "summary_status": "IMPLEMENTED", "summary_skip_reason": "", "summary_llm_calls": 0,
        "persistence_status": "unconfirmed", "reports_status": "not_attempted", "report_paths": {},
        "report_receipts": {"reproduce.sh": {"status": "skipped", "reason": "NO_EXECUTION_ATTEMPT"}},
        "secondary_failures": []}
    value["scope"]["task_id"] = task_id
    value["stop_id"] = "managed-stop:" + _hash({"scope": scope, "authority": value["authority"], "stop_reason": value["stop_reason"]})
    value["explanation"] = explanation(value)
    return value


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
    # A newly CREATED/STARTED attempt supersedes an old failure packet even
    # before another causal packet has been published.
    from agent_core.plan_tracker import active_attempt
    active = active_attempt(deepcopy(durable), actual["step_id"])
    if active and str(active.get("attempt_id") or "") != str(stop.get("scope", {}).get("attempt_id") or ""):
        return False
    return actual == stop.get("scope") and _authority(durable) == stop.get("authority")


def current_managed_stop(durable):
    stop = _mapping(_mapping(durable.get("task_state")).get("managed_run_stop"))
    return stop if stop.get("producer") == PRODUCER and stop.get("persistence_status") == "committed" and _matches(durable, stop) else {}


def _associated_attempts(durable, packet, packet_ref=""):
    """Project the current failure and its explicitly authorized retry parent.

    No latest/last attempt selection; historical rc never becomes current rc.
    """
    task = _mapping(durable.get("task_state"))
    attempts = _mapping(_mapping(durable.get("provenance_state")).get("attempts"))
    rows = []
    references = []
    for decision, receipt in _mapping(task.get("typed_recovery_receipts")).items():
        receipt = _mapping(receipt)
        envelope = _mapping(receipt.get("execution_envelope"))
        if not packet.get("action_ref") or envelope.get("action_id") != packet.get("action_ref"):
            continue
        parent_ref = str(receipt.get("failure_packet_ref") or "")
        parent = _mapping(_mapping(_mapping(task.get("causal_failure_packets")).get(parent_ref)).get("failure_packet"))
        if (parent.get("attempt_ref") and parent.get("attempt_ref") == _mapping(envelope.get("source_scope")).get("attempt_id")
                and _mapping(receipt.get("directive")).get("action") == "retry_step"
                and _mapping(_mapping(task.get("consumed_failure_packet_refs")).get(parent_ref)).get("decision_id") == decision
                and receipt.get("incident_id") == parent.get("incident_id")
                and _mapping(parent.get("plan_spec_ref")) == _mapping(packet.get("plan_spec_ref"))
                and parent.get("task_id") == packet.get("task_id") and parent.get("step_id") == packet.get("step_id")):
            references.append((str(parent["attempt_ref"]), "historical_diagnostic", parent_ref))
    references.append((str(packet.get("attempt_ref") or ""), "current_failure", packet_ref))
    for attempt_id, purpose, reference in references:
        if not attempt_id or any(row["attempt_id"] == attempt_id for row in rows):
            continue
        attempt = _mapping(attempts.get(attempt_id))
        if (attempt.get("step_id") != packet.get("step_id") or attempt.get("plan_id") != durable.get("plan_id")
                or attempt.get("plan_version") != durable.get("plan_version")):
            continue
        effect = _mapping(attempt.get("effect_evidence"))
        parser = _mapping(effect.get("parser_receipt"))
        causal = _mapping(_mapping(task.get("causal_failure_packets")).get(reference or task.get("active_failure_packet_ref")))
        report = _mapping(_mapping(_mapping(causal.get("failure_source")).get("payload")).get("completion_report"))
        gate = _mapping(report.get("completion_gate"))
        evidence_satisfied = (gate.get("passed", "UNKNOWN")
            if _mapping(report.get("scope")).get("attempt_id") == attempt_id else "UNKNOWN")
        rows.append({"attempt_id": attempt_id, "action_id": attempt.get("action_id"),
            "use": purpose, "failure_packet_ref": reference, "returncode": attempt.get("returncode"),
            "emission_count": parser.get("emission_count"), "parser_status": parser.get("status", "UNKNOWN"),
            "observation_count": len(effect.get("produces_observations") or []),
            "candidate_validity": "UNKNOWN", "process_succeeded": attempt.get("returncode") == 0 if type(attempt.get("returncode")) is int else "UNKNOWN",
            "formal_evidence_satisfied": evidence_satisfied,
            "step_completed": False if any(row.get("step_id") == packet.get("step_id") and row.get("status") not in {"passed", "done", "completed"} for row in _steps(durable)) else "UNKNOWN",
            "completion": "historical evidence only" if purpose == "historical_diagnostic" else "see formal verification/commit"})
    return rows


def build_managed_stop(task_id, values, durable, boundary):
    """No reader or store is needed to construct this base."""
    outcome = boundary["outcome"]
    payload = _mapping(outcome.get("payload") or outcome.get("blocker"))
    planning = _mapping(payload.get("planning_terminal"))
    if boundary["node"] == "planner" and planning.get("producer") == "planner_terminal_facts_v1":
        return _build_planning_stop(task_id, durable, boundary, planning)
    _ref, record, packet = _active_packet(durable)
    observed = _mapping(outcome.get("scope"))
    fixed_scope = _scope(durable)
    no_execution = (boundary["node"] == "generate" or (
        boundary["node"] == "execute" and payload.get("command_started") is False))
    if no_execution:
        # A candidate rejection is not the earlier execution failure that
        # motivated its generation. Keep absent execution identity absent.
        record, packet = {}, {}
        fixed_scope.update(attempt_id="", episode_id="")
        from agent_core.active_state import current_generation_causal_reference
        parent = current_generation_causal_reference(durable)
        if parent.get("status") == "verified" and parent == payload.get("causal_generation"):
            _ref = parent["failure_packet_ref"]
            record = _mapping(_mapping(_mapping(durable.get("task_state")).get("causal_failure_packets")).get(_ref))
            packet = _mapping(record.get("failure_packet"))
    if durable.get("_load_failed"):
        fixed_scope.update({k: str(observed.get(k) or "") for k in ("task_id", "plan_id", "plan_version", "step_id", "attempt_id")})
    if packet and str(packet.get("step_id") or "") != fixed_scope["step_id"]:
        record, packet = {}, {}
    primary = _mapping(payload.get("primary_cause"))
    latest = _mapping(payload.get("latest_failure") or primary.get("latest_failure"))
    held = _mapping(payload.get("inspection_stop"))
    if held:
        fixed_scope = _scope(durable, held.get("scope"))
        value = deepcopy(held)
    else:
        source = _mapping(record.get("failure_source"))
        seed_packet = packet or {**fixed_scope, "attempt_ref": fixed_scope["attempt_id"],
            "failure_type": "generated_action_contract_error" if boundary["node"] == "generate" else str(outcome.get("type")),
            "error_code": primary.get("error_code") or payload.get("error_code") or payload.get("reason_code"),
            "summary": primary.get("message") or primary.get("summary") or outcome.get("summary")}
        seed = {"failure_packet": seed_packet, "seed_id": "recovery-seed-" + str(record.get("source_fingerprint") or ""),
                "plan_spec_ref": {"plan_id": durable.get("plan_id"), "plan_version": durable.get("plan_version")}}
        episode = _mapping(payload.get("recovery_episode_facts"))
        primary = _mapping(payload.get("primary_cause"))
        episode.setdefault("terminal_error", {"code": payload.get("error_code") or payload.get("reason_code") or payload.get("violation_code") or boundary["node"].upper() + "_" + str(outcome.get("type")).upper(),
            "detail": payload.get("detail") or outcome.get("summary")})
        value = build_inspection_stop(seed=seed, durable=durable, source=source or outcome, episode=episode,
            archive_ref=str(payload.get("recovery_archive_ref") or ""))
    value.update(producer=PRODUCER, status=boundary["status"], scope=fixed_scope, authority=_authority(durable),
        boundary=deepcopy(boundary), summary_status="IMPLEMENTED", summary_skip_reason="", summary_llm_calls=0)
    value["scope"]["task_id"] = task_id
    value["stage"] = value["stop_reason"].get("failure_stage") or payload.get("failure_stage") or ("recovery.inspection" if value["origin"].get("failure_type") == "inspection_required" else str(boundary["node"]))
    value["origin"].setdefault("source_stage", {"verification_outcome":"supervisor", "completion_gate_outcome":"supervisor"}.get(str(record.get("source_kind")), str(record.get("source_kind") or boundary["node"]).removesuffix("_outcome")))
    if not packet:
        value["origin"].update(error_code=primary.get("error_code") or value["stop_reason"]["error_code"],
            summary=_safe(primary.get("message") or primary.get("summary") or outcome.get("summary")))
        if boundary["node"] == "generate":
            value["origin"].update(source_stage="generate", source_owner=_code(
                primary.get("owner") or primary.get("error_owner"), "UNKNOWN"))
            if outcome.get("type") == "blocked":
                value["origin"]["failure_type"] = "blocked"
    if (boundary["node"] == "execute" and outcome.get("type") == "harness_error"
            and payload.get("error_code") in {
                "DIAGNOSTIC_ACTION_CONTRACT_INVALID", "EXECUTE_ACTION_ROLE_MISSING"
            } and payload.get("command_started") is False):
        # This boundary rejects before launch; it is not a Recovery run.
        value["stage"] = "execute"
        value["stop_reason"]["failure_stage"] = "execute"
        if not packet:
            value["origin"].update(source_stage="execute", source_owner="internal_protocol",
                summary=_safe(primary.get("detail") or payload.get("detail") or outcome.get("summary")))
    value["associated_attempts"] = _associated_attempts(durable, packet, _ref)
    value["current_candidate"] = deepcopy(_mapping(payload.get("current_candidate")))
    value["causal_generation"] = deepcopy(_mapping(payload.get("causal_generation")))
    value["causal_execution"] = deepcopy(value["execution"]) if packet else {"status":"UNKNOWN"}
    if packet:
        value["causal_execution"].update(scope={"task_id":task_id,"plan_id":durable.get("plan_id"),
            "plan_version":durable.get("plan_version"),"step_id":packet.get("step_id"),"attempt_id":packet.get("attempt_ref")},
            failure_packet_ref=_ref,read_only=True)
    # Causal tool failures are already admitted observations, independent of
    # the current local round's zero tool calls. Keep bounded exact references.
    admitted = _mapping(_mapping(_mapping(durable.get("task_state")).get("typed_recovery_receipts")).get(
        value["causal_generation"].get("recovery_decision_id")))
    seen = {(row.get("tool_call_id"), row.get("error_code")) for row in value.get("inspection_failures",[])}
    for row in admitted.get("causal_observation_receipts",[]) or []:
        error = _mapping(row.get("error")); identity = (row.get("request_id"), error.get("code"))
        if error and identity not in seen:
            value["inspection_failures"].append({"tool_call_id":_code(row.get("request_id")),"tool":_code(row.get("tool")),
                "error_code":_code(error.get("code")),"evidence_ref":_safe(row.get("source_ref")),"detail":_safe(error.get("detail")),
                "scope":"admitted_causal_history"})
            seen.add(identity)
    value["inspection_failures"] = value.get("inspection_failures",[])[-20:]
    value["inspection_count_scope"] = {"local_tool_calls":value["stop_reason"].get("tool_calls",0),
        "known_causal_errors":len(value["inspection_failures"]),"history_coverage":"bounded_admitted_causal_chain"}
    value["primary_cause"] = deepcopy(primary or value["origin"])
    value["latest_failure"] = deepcopy(latest or value.get("latest_failure") or ((value.get("inspection_failures") or [{}])[-1]))
    if value["stop_reason"].get("error_code") == "RECOVERY_PROVIDER_TRANSPORT_FAILED":
        value["technical_classification"] = "recovery_provider_transport_error"
        value["latest_failure"] = {"error_code": "RECOVERY_PROVIDER_TRANSPORT_FAILED",
            "stage": "recovery.provider_transport", "category": "provider_transport",
            "message": value["stop_reason"].get("detail", ""), "remote_processing_status": "UNKNOWN"}
    value["stop_reason"].update(termination_reason=str(payload.get("termination_reason") or ""),
        budget_receipt=deepcopy(_mapping(payload.get("budget_receipt"))))
    if no_execution:
        value["execution"].update(step_id=fixed_scope["step_id"], attempt_id="", executed=False, returncode=None,
            deterministic_passed=False, validation_status="not_performed", reviewer_disposition="not_performed",
            committed=False, step_status=next((row.get("status") for row in _steps(durable) if row.get("step_id") == fixed_scope["step_id"]), "unknown"))
    task = _mapping(durable.get("task_state"))
    consumptions = _mapping(task.get("semantic_completion_consumptions"))
    value["prior_completed_work"] = []
    for receipt in _mapping(task.get("typed_step_commit_receipts")).values():
        committed = _mapping(_mapping(receipt).get("outcome"))
        facts = _mapping(committed.get("payload"))
        consumed = _mapping(consumptions.get(facts.get("semantic_consumption_ref")))
        evidence_scope = _mapping(consumed.get("scope"))
        if facts.get("completed_step_id") != fixed_scope["step_id"] and facts.get("commit_id") == consumed.get("commit_id") and all(str(evidence_scope.get(k) or "") == fixed_scope[k] for k in ("task_id", "plan_id", "plan_version")):
            value["prior_completed_work"].append({"step_id": facts["completed_step_id"], "commit_id": facts["commit_id"], "receipt": facts.get("receipt"), "committed": True})
    # Read held execution evidence from this invocation; never read script files.
    execution = _mapping(_mapping(values.get("execute_outcome")).get("payload"))
    raw = _mapping(execution.get("execution_record"))
    if not no_execution and raw and str(raw.get("attempt_id") or "") == value["scope"]["attempt_id"]:
        value["execution"].update(executed=True, purpose=_code(raw.get("purpose") or raw.get("execution_purpose"), value["execution"].get("purpose", "UNKNOWN")), returncode=raw.get("returncode", raw.get("exit_code", execution.get("return_code"))),
            stdout=_safe(raw.get("raw_stdout") or raw.get("stdout"), 2000), stderr=_safe(raw.get("raw_stderr") or raw.get("stderr"), 2000))
        script = _safe(raw.get("code"), 32000)
        if script and script != "[redacted]" and len(str(raw.get("code") or "")) <= 32000:
            value["execution"]["command_body"] = script
            value["execution"]["command_body_available"] = True
    elif packet and not no_execution:
        facts = packet.get("diagnostic_facts") or []
        rc = next((_mapping(f.get("metadata")).get("return_code") for f in facts if f.get("kind") == "return_code"), None)
        if isinstance(rc, int):
            value["execution"].update(executed=True, returncode=rc)
    runtime = _mapping(values.get("runtime_recovery_result") or payload.get("runtime_recovery_result"))
    if runtime:
        value["runtime"] = deepcopy(runtime)
        # Runtime/report diagnostics are secondary to the frozen causal failure.
    if boundary["node"] == "consultant":
        value["consultant_invocation_id"] = str(_mapping(task.get("consultant_invocation")).get("invocation_id") or "")
    value["stop_id"] = "managed-stop:" + _hash({**({"consultant_invocation_id": value["consultant_invocation_id"]} if "consultant_invocation_id" in value else {}), "scope": value["scope"], "authority": value["authority"], "boundary": boundary,
        "stop_reason": value["stop_reason"]})
    value["explanation"] = explanation(value)
    return value


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
    if stop.get("current_candidate"):
        parts.append("末次候选：" + _safe(json.dumps(stop["current_candidate"],ensure_ascii=False),2000))
    if stop.get("inspection_count_scope"):
        parts.append("检查计数范围：" + json.dumps(stop["inspection_count_scope"],ensure_ascii=False))
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
    if stop.get("summary_text"):
        parts.append("AI 辅助说明（不改变上述事实）：\n" + stop["summary_text"])
    if stop.get("internal_diagnostic_delivery"):
        parts.append("内部诊断归档：" + _safe(json.dumps(stop["internal_diagnostic_delivery"],ensure_ascii=False),3000)
            + "。内部归档不等于指定文件交付成功。")
    parts.append("总结状态：" + str(stop.get("summary_status") or "IMPLEMENTED") + ("；" + stop["summary_skip_reason"] if stop.get("summary_skip_reason") else ""))
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
        "本轮已发起 Planner 请求：" + str(reason.get("model_turns", 0)) + "；当前连续结构无进展记录：" + str(reason.get("repeat_count", 0)) + "。",
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
    parts.append("总结状态：" + str(stop.get("summary_status") or "IMPLEMENTED") + ("；" + stop["summary_skip_reason"] if stop.get("summary_skip_reason") else ""))
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
    parts.append("没有科学执行 attempt，不生成科学复现脚本；原任务的科学交付尚未完成。")
    if stop.get("secondary_failures"):
        parts.append("附加问题：" + "；".join(str(i.get("error_code") or "UNKNOWN") for i in stop["secondary_failures"]))
    return "\n\n".join(parts)


def projection(stop):
    if not stop or stop.get("resolution") == "stale_scope": return {}
    d = {"error_code": stop["stop_reason"]["error_code"], "stage": stop.get("stage", "outer"), "error_owner": stop.get("stage", "outer").split(".")[0],
         "reason": explanation(stop), "detail": explanation(stop), "stop_id": stop["stop_id"], "origin": stop["origin"],
         "primary_cause": stop.get("primary_cause", {}), "latest_failure": stop.get("latest_failure", {}),
         "prior_completed_work": stop.get("prior_completed_work", []),
         "execution": stop["execution"], "causal_execution":stop.get("causal_execution",{}),
         "current_candidate":stop.get("current_candidate",{}), "inspection_count_scope":stop.get("inspection_count_scope",{}), "inspection_failures": stop.get("inspection_failures", []), "stop_reason": stop["stop_reason"],
         "internal_diagnostic_delivery":deepcopy(stop.get("internal_diagnostic_delivery") or {}),
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


def _save(data_path, value, *, loader, saver, claim=False):
    fresh = loader(data_path, value["scope"]["task_id"])
    if not _matches(fresh, value): raise ValueError("MANAGED_STOP_STALE_SCOPE")
    prior = current_managed_stop(fresh)
    if prior and prior.get("stop_id") != value["stop_id"]: raise ValueError("MANAGED_STOP_IDENTITY_CONFLICT")
    if claim and prior.get("summary_attempted"):
        raise ValueError("SUMMARY_ALREADY_ATTEMPTED")
    set_task_status(fresh, value["status"], value["stop_reason"]["error_code"])
    if value.get("planning") and _mapping(fresh.get("task_state")).get("planning_failure"):
        # The typed graph already chose this legitimate terminal boundary.
        # Closing its matching correction capsule does not decide a new route.
        failure = fresh["task_state"]["planning_failure"]
        failure["status"] = "terminal"
        failure["terminal_diagnostic"] = deepcopy(value["planning"]["terminal_diagnostic"])
    if claim:
        budgets = dict(fresh["task_state"].get("budgets") or {})
        reservations = dict(budgets.get("terminal_summary_reservations") or {})
        if value["stop_id"] in reservations: raise ValueError("SUMMARY_ALREADY_ATTEMPTED")
        reservations[value["stop_id"]] = int(value.get("summary_reserved_tokens") or 0)
        budgets["terminal_summary_reservations"] = reservations
        fresh["task_state"]["budgets"] = budgets
    historical = _mapping(fresh["task_state"].get("managed_run_stop"))
    if historical.get("stop_id") and historical["stop_id"] != value["stop_id"]:
        fresh["task_state"].setdefault("managed_run_stop_history", {})[historical["stop_id"]] = deepcopy(historical)
    fresh["task_state"]["managed_run_stop"] = deepcopy(value)
    saver(data_path, value["scope"]["task_id"], fresh)
    return fresh


async def finalize_managed_stop(agent, task_id, values, boundary, *, loader=load_plan_state, saver=save_plan_state):
    try: durable = loader(agent.data_path, task_id)
    except Exception: durable = {"task_id": task_id, "_load_failed": True}
    if durable.get("load_error"):
        durable = {"task_id": task_id, "_load_failed": True}
    prior = current_managed_stop(durable)
    value = build_managed_stop(task_id, values, durable, boundary)
    if durable.get("_load_failed"):
        value.update(persistence_status="unconfirmed", summary_status="SKIPPED", summary_skip_reason="DURABLE_SCOPE_UNCONFIRMED")
        _secondary(value, "stop.load", "MANAGED_STOP_LOAD_FAILED")
        value["explanation"] = explanation(value)
        return value
    if _mapping(durable.get("task_state")).get("status") in {"completed", "cancelled"}:
        value.update(resolution="stale_scope", persistence_status="not_applied", summary_status="SKIPPED", summary_skip_reason="CURRENT_TASK_TERMINAL")
        return value
    if value.get("planning") and not _matches(durable, value):
        value.update(resolution="stale_scope", persistence_status="not_applied", summary_status="SKIPPED", summary_skip_reason="STALE_SCOPE")
        return value
    observed_scope = _mapping(boundary.get("outcome", {}).get("scope"))
    for key in ("task_id", "plan_id", "plan_version", "step_id", "task_revision", "plan_revision", "planning_epoch_id", "failure_fingerprint", "candidate_context_ref"):
        current = value["scope"].get(key, value["authority"].get(key))
        if observed_scope.get(key) and str(observed_scope[key]) != str(current):
            value.update(resolution="stale_scope", persistence_status="not_applied", summary_status="SKIPPED", summary_skip_reason="STALE_SCOPE")
            return value
    if prior:
        if prior.get("reports_status") == "finalized": return prior
        value = deepcopy(prior)
    if value.get("planning") and value["scope"].get("candidate_context_ref"):
        # Optional enrichment only: base contains the complete durable issues
        # and original cause before this immutable object read can fail.
        try:
            from agent_core.state_contract_store import StateContractStore
            record = StateContractStore(agent.data_path, task_id).read_object(
                value["scope"]["candidate_context_ref"], expected_type="planner_correction_context")
            if (record.get("task_id") != task_id
                or record.get("planning_epoch_id") != value["scope"]["planning_epoch_id"]
                or record.get("task_semantic_hash") != value["planning"]["task_semantic_hash"]):
                raise ValueError("PLANNER_CONTEXT_SCOPE_MISMATCH")
            value["planning"]["context_read_status"] = "verified"
        except Exception as exc:
            value["planning"]["context_read_status"] = "unavailable"
            _secondary(value, "planning.details", "PLANNER_CONTEXT_DETAILS_UNAVAILABLE", exc)
    # Base is already constructed. Optional execution detail I/O can enrich
    # it, but cannot become a prerequisite for a truthful terminal explanation.
    raw_execution = _mapping(_mapping(values.get("execute_outcome")).get("payload")).get("execution_record") or {}
    reference = _mapping(raw_execution).get("recovery_execution_facts_ref")
    if reference:
        try:
            from agent_core.state_contract_store import StateContractStore
            from agent_core.workflow.inspection_evidence import read_execution_facts
            store = StateContractStore(agent.data_path, task_id)
            facts = read_execution_facts(store, reference)
            target_execution = value.get("causal_execution") if value.get("current_candidate") and value.get("causal_execution",{}).get("attempt_id") else value["execution"]
            detail_scope = target_execution.get("scope") or value["scope"]
            for key in ("task_id", "plan_id", "plan_version", "step_id", "attempt_id"):
                if str(facts.get(key) or "") != str(detail_scope[key]): raise ValueError("EXECUTION_FACT_SCOPE_MISMATCH")
            for key in ("stdout", "stderr"):
                target_execution[key] = _safe(facts.get("raw_"+key) or facts.get(key), 2000)
            code = _safe(facts.get("code"), 32000)
            if code and code != "[redacted]" and len(str(facts.get("code") or "")) <= 32000:
                target_execution.update(command_body=code, command_body_available=True)
        except Exception as exc:
            _secondary(value, "execution.details", "EXECUTION_DETAILS_UNAVAILABLE", exc)
    # Base exists before the first write; failures below never erase it.
    try:
        value["persistence_status"] = "committed"
        durable = _save(agent.data_path, value, loader=loader, saver=saver)
    except Exception as exc:
        value["persistence_status"] = "unconfirmed"
        _secondary(value, "stop.persist", "MANAGED_STOP_PERSIST_FAILED", exc)
        value["summary_status"], value["summary_skip_reason"] = "SKIPPED", "DURABLE_CLAIM_UNCONFIRMED"
        value["explanation"] = explanation(value)
        return value
    policy = dict(getattr(agent, "terminal_summary_policy", {}) or {})
    transport = getattr(agent, "terminal_summary_llm", None)
    skip = ""
    if policy.get("enabled", True) is not True: skip = "CONFIG_DISABLED"
    elif value.get("summary_attempted"): skip = "ALREADY_ATTEMPTED"
    elif transport is None: skip = "NO_AUTHORIZED_PROVIDER"
    elif policy.get("provider_available", True) is not True: skip = "PROVIDER_UNAVAILABLE"
    elif policy.get("requests_allowed", True) is not True: skip = "REQUESTS_NOT_PERMITTED"
    elif float(policy.get("deadline", time.time() + 60)) - time.time() < float(policy.get("timeout_seconds", 30)): skip = "GLOBAL_DEADLINE_INSUFFICIENT"
    # Conservative reservation (UTF-8 byte bound plus maximum completion).
    frozen = {k: value[k] for k in ("origin", "primary_cause", "latest_failure", "stop_reason", "execution", "inspection_failures", "stages", "prior_completed_work")}
    if value.get("planning"):
        # The summary gets every issue identity/location, with its exact full
        # diagnostic retained durably in planning. Avoid duplicating the same
        # large source analysis in origin, latest and base in a bounded call.
        frozen["primary_cause"] = {k: v for k, v in value["primary_cause"].items() if k not in {"source", "validation_issues", "response_integrity"}}
        frozen["latest_failure"] = {k: v for k, v in value["latest_failure"].items() if k not in {"validation_issues", "response_integrity"}}
        frozen["response_integrity"] = {"origin": _planning_response_projection(value["primary_cause"].get("response_integrity")),
            "latest": _planning_response_projection(value["latest_failure"].get("response_integrity"))}
        frozen["first_truncation"] = _planning_response_projection(value.get("first_truncation"))
        frozen["output_policy"] = {"origin": deepcopy(_planning_policy(value["primary_cause"].get("response_integrity"))),
            "latest": deepcopy(value.get("output_policy") or {}),
            "first_truncation": deepcopy(_planning_policy(value.get("first_truncation")))}
        frozen["attempted_remediation"] = deepcopy(value.get("attempted_remediation") or [])
        frozen["files"] = {name: deepcopy(value["report_receipts"].get(name) or {"status": "not_attempted", "reason": "WRITER_NOT_RUN"})
            for name in ("report.md", "stage_status.tsv", "reproduce.sh")}
        frozen["planning"] = {"scope": value["scope"], "no_approved_plan": True,
            "validation_issues": [{k: i[k] for k in ("code", "violation_code", "field_path", "canonical_resource_id", "affected_step_ids", "constraint_source", "explicit_contract_source", "contract_revision",
                "expected_type", "received_type", "position", "line", "column", "diagnostic_source", "position_source", "validator_id") if k in i}
                for i in value["planning"].get("validation_issues") or []]}
    frozen["execution"] = {k:v for k,v in frozen["execution"].items() if k != "command_body"}
    frozen["causal_execution"] = {k:v for k,v in value.get("causal_execution",{}).items() if k != "command_body"}
    frozen["current_candidate"] = deepcopy(value.get("current_candidate") or {})
    frozen["inspection_count_scope"] = deepcopy(value.get("inspection_count_scope") or {})
    frozen["base"] = explanation(value)
    frozen["associated_attempts"] = deepcopy(value.get("associated_attempts") or [])
    frozen["inspection_observations"] = deepcopy(value.get("inspection_observations") or [])
    wire = json.dumps(frozen, ensure_ascii=False)
    reserve = len(wire.encode()) + 1500
    if len(wire) > 20000: skip = skip or "FACT_BUDGET_EXCEEDED"
    if int(policy.get("remaining_tokens", reserve)) < reserve: skip = skip or "GLOBAL_TOKEN_BUDGET_INSUFFICIENT"
    if "max_total_tokens" in policy:
        try:
            used = int(agent.profiler.snapshot(task_id).get("tokens", {}).get("total_tokens") or 0)
            if used + reserve > int(policy["max_total_tokens"]): skip = skip or "GLOBAL_TOKEN_BUDGET_INSUFFICIENT"
        except Exception: skip = skip or "GLOBAL_BUDGET_UNCONFIRMED"
    if "remaining_cost" in policy:
        if "reserved_cost" not in policy: skip = skip or "SUMMARY_COST_BOUND_UNKNOWN"
        elif float(policy["remaining_cost"]) < float(policy["reserved_cost"]): skip = skip or "GLOBAL_COST_BUDGET_INSUFFICIENT"
    if skip:
        value["summary_status"], value["summary_skip_reason"] = "SKIPPED", skip
    else:
        try:
            value.update(summary_attempted=True, summary_status="ATTEMPTED", summary_llm_calls=1, summary_reserved_tokens=reserve)
            _save(agent.data_path, value, loader=loader, saver=saver, claim=True)
        except Exception as exc:
            value["summary_status"], value["summary_skip_reason"] = "SKIPPED", "DURABLE_CLAIM_UNCONFIRMED"
            value["summary_llm_calls"] = 0
            _secondary(value, "summary.claim", "SUMMARY_CLAIM_FAILED", exc)
        else:
            value["summary_llm_calls"] = 1
            try:
                response = await agent._ainvoke_llm([SystemMessage(content=SUMMARY_PROMPT), HumanMessage(content=wire)],
                    stage="terminal_summary", task_id=task_id, task_mode="readonly", llm_override=transport,
                    llm_policy={"sdk_max_retries": 0, "application_max_attempts": 1, "completion_cache_enabled": False,
                        "total_timeout_seconds": min(30, float(policy.get("timeout_seconds", 30)))},
                    trace_metadata={"call_kind": "terminal_summary", "semantic_invocation_id": value["stop_id"],
                        "step_id":value["scope"].get("step_id",""), "frozen_stop_scope":deepcopy(value["scope"])})
            except asyncio.CancelledError: raise
            except Exception as exc:
                value["summary_status"] = "FAILED"
                _secondary(value, "summary.provider", "SUMMARY_FAILED", exc)
                value["summary_cleanup"] = str(getattr(getattr(exc, "record", None), "cleanup_result", "not_observed"))
            else:
                try:
                    if getattr(response, "tool_calls", None) or _mapping(getattr(response, "additional_kwargs", {})).get("tool_calls"):
                        raise ValueError("SUMMARY_TOOL_CALL_FORBIDDEN")
                    parsed = json.loads(response.content)
                    if (not isinstance(parsed, dict) or set(parsed) not in ({"fact_ids"}, {"explanation", "fact_ids"})
                            or ("explanation" in parsed and (not isinstance(parsed["explanation"], str) or not parsed["explanation"].strip() or len(parsed["explanation"]) > 4000))
                            or not isinstance(parsed.get("fact_ids"), list) or not parsed["fact_ids"]):
                        raise ValueError("SUMMARY_INVALID_STRUCTURE")
                except Exception as exc:
                    value["summary_status"] = "FAILED"
                    _secondary(value, "summary.parse", "SUMMARY_RESPONSE_INVALID", exc)
                else:
                    try:
                        if any(not isinstance(k, str) or k not in frozen or k == "base" for k in parsed["fact_ids"]):
                            raise ValueError("SUMMARY_UNDECLARED_FACT")
                        # Free prose cannot attest scientific completion or
                        # nonexistence. Neither negative nor positive wording
                        # is classified by a substring blacklist. The single
                        # provider selects frozen facts; deterministic rendering
                        # retains their value and uncertainty without new claims.
                        value.update(summary_status="COMPLETED", summary_consumption_status="GROUNDED_FACT_SELECTION",
                                     summary_selected_fact_ids=list(dict.fromkeys(parsed["fact_ids"])),
                                     summary_unverified_prose=_safe(parsed.get("explanation") or "", 4000))
                        value["summary_text"] = "\n".join("已记录 " + key + "：" + _safe(json.dumps(frozen[key], ensure_ascii=False, sort_keys=True, default=str), 1800) for key in value["summary_selected_fact_ids"])
                    except Exception as exc:
                        value["summary_status"] = "FAILED"
                        _secondary(value, "summary.facts", "SUMMARY_FACT_CONSUMPTION_FAILED", exc)
    # Persist summary result before report publication; reload never reissues it.
    try: durable = _save(agent.data_path, value, loader=loader, saver=saver)
    except Exception as exc: _secondary(value, "summary.persist", "SUMMARY_RECEIPT_PERSIST_FAILED", exc)
    try:
        if value.get("planning"):
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}", str(task_id)):
                raise ValueError("MANAGED_STOP_TASK_ID_REJECTED")
            task_root = _path(Path(agent.data_path).absolute() / "task_outputs" / task_id)
            targets = {name: _path(task_root / name) for name in ("report.md", "stage_status.tsv")}
            spec = canonical_task_spec_for_state(durable)
            inputs = {str(i.get("canonical_path") or i.get("path") or "") for i in spec.get("inputs") or [] if isinstance(i, Mapping)}
            if any(str(target) in inputs for target in targets.values()):
                raise ValueError("MANAGED_STOP_REPORT_INPUT_OVERLAP")
            value["report_receipts"]["reproduce.sh"] = {"status": "skipped", "reason": "NO_EXECUTION_ATTEMPT"}
        else:
            targets = _report_targets(durable, Path(agent.data_path).absolute() / "task_outputs" / task_id)
        bodies = _report_bodies(value)
        rows = ["stage\tworkflow_status\texecution_status\tcommitted\tnotes"]
        execution = value.get("causal_execution") if value.get("causal_execution",{}).get("attempt_id") else value["execution"]
        for stage in value["stages"]:
            current = stage["step_id"] == value["scope"]["step_id"]
            observed = ("failed" if execution.get("returncode") not in (None, 0) else "succeeded" if execution.get("returncode") == 0 else "not_started_or_unknown") if current else ("completed" if stage["status"] in {"done", "passed", "completed"} else "not_started_or_unknown")
            rows.append("\t".join((stage["step_id"],stage["status"],observed,str(bool(execution.get("committed"))) if current else str(any(row["step_id"] == stage["step_id"] for row in value.get("prior_completed_work", []))), ("latest attempt purpose=" + str(execution.get("purpose") or "UNKNOWN") + "; primary causal returncode=" + str(value.get("origin", {}).get("returncode")) + "; formal verification/commit remain authoritative") if current else "formal verification/commit remain authoritative")))
        rows.append("run\t" + value["status"] + "\tstopped\tfalse\t" + value["stop_reason"]["error_code"])
        bodies["stage_status.tsv"] = "\n".join(rows) + "\n"
        bodies["report.md"] = "# BioMind managed run stop\n\nStop identity: " + value["stop_id"] + "\n\n" + explanation(value) + "\n"
        if execution.get("command_body_available"):
            # Preserve the actual command as commented evidence. A failed
            # action is never silently turned into a runnable success recipe.
            bodies["reproduce.sh"] = "#!/bin/sh\n# Recorded action evidence; no replay is authorized by this report.\n# Stop: " + value["stop_id"]
            bodies["reproduce.sh"] += "\n# Causal attempt: " + str(execution.get("attempt_id") or "") + "; rc=" + str(execution.get("returncode"))
            bodies["reproduce.sh"] += "\n" + "\n".join("# | " + line for line in execution["command_body"].splitlines()) + "\nexit 1\n"
        if value.get("frozen_report_bodies"):
            bodies = deepcopy(value["frozen_report_bodies"])
        else:
            value["frozen_report_bodies"] = deepcopy(bodies)
            durable = _save(agent.data_path, value, loader=loader, saver=saver)
        for name in ("stage_status.tsv", "reproduce.sh", "report.md"):
            path = targets.get(name)
            if not path:
                value["report_receipts"].setdefault(name, {"status": "skipped", "reason": "NO_APPROVED_OUTPUT"})
                continue
            try:
                if not _matches(loader(agent.data_path, task_id), value): raise ValueError("MANAGED_STOP_STALE_SCOPE")
                receipt = publish_report(agent.data_path, task_id, path, bodies[name], owner=value["stop_id"],
                    authorization_ref=_hash(value["authority"]), scope_guard=lambda fresh: _matches(fresh,value))
                value["report_paths"][name] = str(path)
                value["report_receipts"][name] = {**receipt, "status": "created", "stop_id": value["stop_id"], "task_id": task_id}
            except Exception as exc:
                value["report_receipts"][name] = {"status": "failed", "path": str(path), "task_id": task_id, "stop_id": value["stop_id"], "reason": _code(getattr(exc, "code", ""), "REPORT_WRITE_FAILED")}
                _secondary(value, "report." + name, value["report_receipts"][name]["reason"], exc)
    except Exception as exc:
        _secondary(value, "report.contract", "REPORT_CONTRACT_FAILED", exc)
    value["internal_diagnostic_delivery"] = {"status":"not_needed","paths":{},"receipts":{}}
    if report_delivery_status(value.get("report_receipts")) != "complete":
        internal=value["internal_diagnostic_delivery"]
        try:
            fresh=loader(agent.data_path,task_id)
            targets, authorization=_internal_report_targets(fresh, Path(agent.data_path).absolute()/"task_outputs"/task_id,value["stop_id"])
            if not targets:
                internal.update(status="not_authorized",reason=authorization)
            else:
                for name,path in targets.items():
                    try:
                        receipt=publish_report(agent.data_path,task_id,path,value["frozen_report_bodies"][name],
                            owner=value["stop_id"]+":internal",authorization_ref=authorization,
                            scope_guard=lambda current: _matches(current,value) and _internal_report_targets(current,
                                Path(agent.data_path).absolute()/"task_outputs"/task_id,value["stop_id"])==(targets,authorization))
                        internal["paths"][name]=str(path);internal["receipts"][name]={**receipt,"status":"created"}
                    except Exception as exc:
                        internal["receipts"][name]={"status":"failed","reason":_code(getattr(exc,"code",""),"INTERNAL_REPORT_WRITE_FAILED")}
                        _secondary(value,"internal_report."+name,internal["receipts"][name]["reason"],exc)
                internal["status"]=report_delivery_status(internal["receipts"])
        except Exception as exc:
            internal.update(status="failed",reason="INTERNAL_REPORT_AUTHORIZATION_OR_WRITE_FAILED")
            _secondary(value,"internal_report",internal["reason"],exc)
    value["reports_status"] = "finalized"
    value["delivery_status"] = report_delivery_status(value.get("report_receipts"))
    value["explanation"] = explanation(value)
    try: _save(agent.data_path, value, loader=loader, saver=saver)
    except Exception as exc: _secondary(value, "report.persist", "REPORT_RECEIPT_PERSIST_FAILED", exc)
    return value


def emergency_projection(values, error):
    """In-memory fallback after an already confirmed managed terminal boundary."""
    boundary = _mapping(values.get("managed_run_boundary"))
    outcome = _mapping(boundary.get("outcome"))
    payload = _mapping(outcome.get("payload"))
    planning = _mapping(payload.get("planning_terminal"))
    if planning.get("producer") == "planner_terminal_facts_v1":
        value = _build_planning_stop(str(_mapping(planning.get("scope")).get("task_id") or ""), {},
            {"node": "planner", "status": payload.get("status", "failed"), "outcome": outcome}, planning)
        value.update(summary_status="SKIPPED", summary_skip_reason="DIAGNOSTIC_INCOMPLETE")
        _secondary(value, "stop.projection", "MANAGED_STOP_PROJECTION_FAILED", error)
        return projection(value)
    held = _mapping(payload.get("inspection_stop"))
    reason = _mapping(held.get("stop_reason")).get("error_code") or payload.get("error_code") or payload.get("reason_code") or "MANAGED_TERMINAL_DIAGNOSTIC_INCOMPLETE"
    text = _safe(held.get("explanation"), 20000) or _safe(outcome.get("summary")) or "当前 managed run 已停止；停止诊断不完整。"
    text += "\n\n停止说明持久化/报告尚未确认；本次使用内存事实。附加错误：" + type(error).__name__
    return {"status": "failed", "terminal_diagnostic": {"error_code":reason, "stage":boundary.get("node") or "outer",
        "error_owner":"outer", "reason":text, "detail":text, "persistence_status":"unconfirmed",
        "summary_status":"SKIPPED", "summary_skip_reason":"DIAGNOSTIC_INCOMPLETE", "report_paths":{}}}
