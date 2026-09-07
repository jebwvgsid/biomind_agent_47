"""Bounded pre-generation runtime acquisition inside the existing Recovery node.

This is a route-blind data/inspection service. It never invokes a model, installs
software, changes environments, executes a scientific action or approves a plan.
The injected inspector is the sole environment leaf; persisted request contracts
restrict its scope. LangGraph consumes the returned semantic disposition.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence

from langgraph.errors import GraphBubbleUp

from agent_core.action_runtime import (
    ActionRuntimeContractError, build_runtime_binding_registry,
    capability_contract_hash, normalize_capability_contract,
    resolve_action_runtime_bindings,
)
from agent_core.plan_contract import StateCASConflict
from agent_core.plan_tracker import canonical_task_spec_for_state, load_plan_state, save_plan_state, semantic_plan_hash
from agent_core.state_contract_store import StateContractStore
from agent_core.task_spec import semantic_constraints_from_task_spec
from agent_core.runtime_evidence import (
    RuntimeEvidenceError, SCOPE_FIELDS, digest, evaluate_runtime_evidence,
    make_runtime_evidence, merge_runtime_evidence, project_runtime_audits,
    runtime_evidence_state, validate_runtime_evidence,
)
from agent_core.workflow.outcomes import workflow_routing_schema_version

_PRODUCER = "pre_generation_runtime_recovery_v1"
_TERMINAL = frozenset({"unresolved", "dependency_blocked", "internal_error", "needs_user_decision"})
_SAFE_CODE = re.compile(r"^[a-zA-Z0-9_.:-]{1,160}$")
_LOCKS: dict[tuple[str, str, str], asyncio.Lock] = {}


@dataclass(frozen=True)
class RuntimeRecoveryPolicy:
    max_actions: int = 3
    max_inspections: int = 2
    max_transient_retries: int = 1
    inspection_deadline_seconds: float = 60.0
    episode_deadline_seconds: float = 180.0
    max_llm_decisions: int = 2
    max_explanation_calls: int = 1

    def __post_init__(self):
        for field in ("max_actions", "max_inspections", "max_transient_retries", "max_llm_decisions", "max_explanation_calls"):
            value = getattr(self, field)
            if type(value) is not int or value < 0:
                raise ValueError("RUNTIME_RECOVERY_POLICY_INVALID: " + field)
        if self.max_actions > 3 or self.max_inspections > 2 or self.max_transient_retries > 1 or self.max_llm_decisions > 2 or self.max_explanation_calls > 1:
            raise ValueError("RUNTIME_RECOVERY_POLICY_EXCEEDS_LOCAL_LIMIT")
        if not 0 < self.inspection_deadline_seconds <= 60 or not 0 < self.episode_deadline_seconds <= 180:
            raise ValueError("RUNTIME_RECOVERY_DEADLINE_INVALID")


class RuntimeRecoveryError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _code(value: Any, fallback: str) -> str:
    text = str(value or "")
    return text if _SAFE_CODE.fullmatch(text) else fallback


def _time(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RuntimeRecoveryError("RUNTIME_RECOVERY_TIMEZONE_REQUIRED")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _task_state(durable: Mapping[str, Any]) -> dict[str, Any]:
    return deepcopy(dict(durable.get("task_state") or {}))


def runtime_authorization_scope_digest(durable: Mapping[str, Any]) -> str:
    """One pure authorization identity for acquisition and report acceptance."""
    authorization = {
        key: deepcopy(durable.get(key)) for key in (
            "approval_scope", "approval_status", "approved_plan_version",
            "approval_record", "approval_projection", "current_approval",
            "approval_snapshot", "approval_snapshot_ref", "approval_record_ref",
            "approved_plan_hash", "plan_snapshot_ref",
        ) if durable.get(key) not in (None, {}, "")
    }
    authorization["current_plan_refs"] = {
        key: deepcopy((durable.get("current_plan") or {}).get(key))
        for key in ("plan_snapshot_ref", "plan_record_ref", "plan_semantic_hash")
        if (durable.get("current_plan") or {}).get(key) not in (None, "")
    }
    authorization["task_constraints"] = [
        {key: deepcopy(row.get(key)) for key in ("semantic_key", "value", "scope", "required", "operator")}
        for row in semantic_constraints_from_task_spec(canonical_task_spec_for_state(durable))
    ]
    return digest(authorization)


def _current_step(durable: Mapping[str, Any]) -> dict[str, Any]:
    sid = str(durable.get("current_step_id") or (durable.get("task_state") or {}).get("current_step_id") or "")
    step = next((deepcopy(dict(row)) for row in durable.get("steps", []) if isinstance(row, Mapping) and str(row.get("step_id") or "") == sid), {})
    if not sid or not step:
        raise RuntimeRecoveryError("RUNTIME_RECOVERY_CURRENT_STEP_REQUIRED")
    return step


def _not_dispatched(durable: Mapping[str, Any], step_id: str) -> bool:
    provenance = dict(durable.get("provenance_state") or {})
    for row in (provenance.get("attempts") or {}).values():
        if isinstance(row, Mapping) and str(row.get("step_id") or "") == step_id:
            # Even a prepared attempt is owned by the existing execution
            # lifecycle. Runtime acquisition must not reclassify it.
            return False
    for row in durable.get("execution_history", []) or []:
        if isinstance(row, Mapping) and str(row.get("step_id") or "") == step_id:
            return False
    return True


class RuntimeEvidenceRecovery:
    def __init__(
        self, *, data_path: str, scope_provider: Callable[[str], Mapping[str, Any]],
        inspect_runtime: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
        inspection_contract_provider: Callable[[str], Mapping[str, Any]] | None = None,
        policy: RuntimeRecoveryPolicy | None = None,
        clock: Callable[[], datetime | str] | None = None,
    ) -> None:
        self.data_path = str(data_path)
        self.scope_provider = scope_provider
        self.inspect_runtime = inspect_runtime
        self.inspection_contract_provider = inspection_contract_provider
        self.policy = policy or RuntimeRecoveryPolicy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.owner_id = "runtime-worker:" + uuid.uuid4().hex

    def _now(self) -> datetime:
        return _time(self.clock())

    def _subject(self, skill_id: str) -> dict[str, str]:
        raw = dict(self.scope_provider(skill_id) or {})
        subject = {key: str(raw.get(key) or "") for key in SCOPE_FIELDS}
        if subject["skill_id"] != skill_id:
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_SKILL_SCOPE_MISMATCH")
        return subject

    def _scope(self, durable: Mapping[str, Any], skill_id: str, contract: Mapping[str, Any]) -> dict[str, Any]:
        if workflow_routing_schema_version(durable) != 2:
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_REQUIRES_SCHEMA2")
        step = _current_step(durable)
        step_id = str(step["step_id"])
        if not _not_dispatched(durable, step_id):
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_EXECUTION_ALREADY_OWNED")
        task = _task_state(durable)
        if str(task.get("status") or durable.get("task_status") or "").lower() == "cancelled":
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_CANCELLED")
        skills = step.get("skill_ids") or step.get("skills") or task.get("selected_skills") or durable.get("selected_skill_ids") or []
        if not isinstance(skills, (list, tuple)) or skill_id not in skills:
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_SKILL_NOT_SELECTED")
        capability_id = str(contract.get("capability_id") or "")
        declared_capability = str(step.get("execution_capability_id") or "")
        if declared_capability and declared_capability != capability_id:
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_CAPABILITY_SCOPE_MISMATCH")
        declared_hash = str(step.get("capability_contract_hash") or "")
        contract_hash = capability_contract_hash(contract)
        if declared_hash and declared_hash != contract_hash:
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_CAPABILITY_CONTRACT_DRIFT")
        return {
            "task_id": str(durable.get("task_id") or ""),
            "plan_id": str(durable.get("plan_id") or ""),
            "plan_version": str(durable.get("plan_version") or ""),
            "semantic_plan_hash": semantic_plan_hash(durable),
            "step_id": step_id,
            "step_revision": digest({key: step.get(key) for key in (
                "step_id", "execution_capability_id", "capability_contract_hash", "skill_ids", "skills",
            )}),
            "selected_skill_id": skill_id,
            "execution_capability_id": capability_id,
            "capability_contract_digest": contract_hash,
            "required_binding_ids": list(contract.get("required_runtime_binding_ids") or []),
            "subject": self._subject(skill_id),
            "authorization_scope_digest": runtime_authorization_scope_digest(durable),
        }

    def _inspection_contract(self, skill_id: str, subject: Mapping[str, Any], supplied: Mapping[str, Any] | None = None) -> dict[str, Any]:
        raw = dict(supplied or (self.inspection_contract_provider(skill_id) if self.inspection_contract_provider else {}) or {})
        if not raw:
            raw = dict((self.scope_provider(skill_id) or {}).get("inspection_contract") or {})
        if not raw:
            return {}
        clean = {key: deepcopy(raw.get(key)) for key in (
            "contract_id", "contract_digest", "method_digest", "permitted", "safe_repeat", "coverage_binding_ids", "cache_restore_permitted", "reason_code",
        )}
        clean["method_digest"] = str(clean.get("method_digest") or clean.get("contract_digest") or "")
        clean["coverage_binding_ids"] = sorted(set(str(value) for value in clean.get("coverage_binding_ids") or []))
        clean["deadline_seconds"] = min(float(raw.get("deadline_seconds") or self.policy.inspection_deadline_seconds), self.policy.inspection_deadline_seconds)
        if not clean.get("contract_id") or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(clean.get("contract_digest") or "")) or clean.get("method_digest") != subject.get("inspection_contract_digest") or type(clean.get("permitted")) is not bool or not 0 < clean["deadline_seconds"] <= 60:
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_INSPECTION_CONTRACT_INVALID")
        clean["safe_repeat"] = clean.get("safe_repeat") is True
        clean["cache_restore_permitted"] = clean.get("cache_restore_permitted") is True
        clean["reason_code"] = _code(clean.get("reason_code"), "RUNTIME_INSPECTION_SAFETY_UNPROVEN") if not clean["permitted"] else ""
        return clean

    def _assessment(self, durable: Mapping[str, Any], scope: Mapping[str, Any], contract: Mapping[str, Any]) -> dict[str, Any]:
        task = _task_state(durable)
        audits = project_runtime_audits(task, task_id=scope["task_id"], skill_scope=scope["subject"], now=_stamp(self._now()))
        facts = evaluate_runtime_evidence(task, task_id=scope["task_id"], skill_scope=scope["subject"], required_binding_ids=scope["required_binding_ids"], now=_stamp(self._now()))
        registry = build_runtime_binding_registry(audits)
        # Explicit fact outcomes dominate a derived compatibility path. This is
        # generic: e.g. a negative executable fact cannot be bypassed because a
        # separate home/directory observation can still derive its filename.
        for binding_id, fact in facts.items():
            if fact["availability"] in {"UNAVAILABLE", "AMBIGUOUS"} or (fact["availability"] == "UNKNOWN" and fact["evidence_ids"]):
                registry.pop(binding_id, None)
        for binding_id in scope["required_binding_ids"]:
            row = registry.get(binding_id) or {}
            if row.get("status") == "ready":
                facts[binding_id] = {**facts.get(binding_id, {}), "availability": "AVAILABLE", "reason_code": "verified_materialization"}
        semantic = {
            binding_id: {"availability": facts[binding_id]["availability"], "reason_code": facts[binding_id]["reason_code"], "value_digest": (registry.get(binding_id) or {}).get("value_digest", "")}
            for binding_id in scope["required_binding_ids"]
        }
        ready = False
        resolution = {}
        try:
            resolution = resolve_action_runtime_bindings(
                selected_skill_id=scope["selected_skill_id"],
                execution_capability_id=scope["execution_capability_id"],
                approved_capability_contract_hash=scope["capability_contract_digest"],
                capability_registry={scope["execution_capability_id"]: contract},
                ready_runtime_binding_registry=registry,
                current_action_identity={"plan_id": scope["plan_id"], "step_id": scope["step_id"]},
            )
            ready = True
        except ActionRuntimeContractError as exc:
            if getattr(exc, "code", "") != "REQUIRED_RUNTIME_BINDING_UNAVAILABLE":
                raise
        return {"ready": ready, "facts": facts, "runtime_audit": audits, "resolution": resolution,
                "evidence_semantic_digest": digest(semantic),
                "consumer_projection_digest": digest({key: {name: row.get(name) for name in ("status", "value_digest", "source_skill_id", "env_name")} for key, row in registry.items()})}

    def create_request(self, state: Mapping[str, Any], *, selected_skill_id: str, capability_contract: Mapping[str, Any], missing_binding_ids: Sequence[str], original_error: Mapping[str, Any] | str, inspection_contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
        task_id = str(state.get("task_id") or "")
        if not task_id:
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_TASK_REQUIRED")
        durable = load_plan_state(self.data_path, task_id)
        contract = normalize_capability_contract(capability_contract, selected_skill_id=selected_skill_id)
        required = list(contract["required_runtime_binding_ids"])
        if not required or not set(missing_binding_ids) <= set(required):
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_REQUIRED_FACT_SCOPE_INVALID")
        scope = self._scope(durable, selected_skill_id, contract)
        for field, expected in (("plan_id", scope["plan_id"]), ("plan_version", scope["plan_version"]), ("current_step_id", scope["step_id"])):
            if state.get(field) not in (None, "") and str(state[field]) != expected:
                raise RuntimeRecoveryError("RUNTIME_RECOVERY_GENERATE_SCOPE_STALE")
        assessment = self._assessment(durable, scope, contract)
        episode_id = "runtime-episode:" + digest(scope)
        task = _task_state(durable)
        recovery = deepcopy(dict(task.get("runtime_recovery") or {"schema_version": 1, "episodes": {}}))
        episodes = recovery.setdefault("episodes", {})
        episode = deepcopy(dict(episodes.get(episode_id) or {}))
        if episode:
            # Request IDs and user phrasing cannot reset a durable episode.
            request = deepcopy(episode["request"])
            if request["scope"] != scope:
                raise RuntimeRecoveryError("RUNTIME_RECOVERY_EPISODE_IDENTITY_CONFLICT")
        else:
            permitted = self._inspection_contract(selected_skill_id, scope["subject"], inspection_contract)
            reason = _code(original_error.get("error_code") or original_error.get("code"), "REQUIRED_RUNTIME_BINDING_UNAVAILABLE") if isinstance(original_error, Mapping) else _code(original_error, "REQUIRED_RUNTIME_BINDING_UNAVAILABLE")
            request = {
                "schema_version": 1, "producer": _PRODUCER,
                "request_id": "runtime-request:" + digest(scope), "episode_id": episode_id,
                "scope": scope, "capability_contract": contract,
                "inspection_contract": permitted,
                "required_binding_ids": required,
                "missing_binding_ids": sorted(set(missing_binding_ids)),
                "original_error_code": reason, "execution_state": "not_dispatched",
                "task_revision_at_creation": int(durable.get("task_revision") or (durable.get("current_task") or {}).get("task_revision") or 0),
            }
            request["content_digest"] = digest(request)
            episode = {
                "episode_id": episode_id, "request": request, "started_at": _stamp(self._now()),
                "status": "pending", "reason_code": reason, "failure_class": "runtime_evidence_missing",
                "policy": asdict(self.policy), "actions_used": 0, "inspections_used": 0,
                "transient_retries_used": 0, "llm_decisions_used": 0, "explanation_calls_used": 0,
                "elapsed_seconds": 0.0, "receipts": [],
                "initial_evidence_semantic_digest": assessment["evidence_semantic_digest"],
                "last_evidence_semantic_digest": assessment["evidence_semantic_digest"],
                "last_consumer_projection_digest": assessment["consumer_projection_digest"],
            }
            episodes[episode_id] = episode
        recovery["active_episode_id"] = episode_id
        task["runtime_recovery"] = recovery
        durable["task_state"] = task
        save_plan_state(self.data_path, task_id, durable)
        return deepcopy(request)

    def _load_request(self, state: Mapping[str, Any], requested: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        task_id = str(state.get("task_id") or "")
        durable = load_plan_state(self.data_path, task_id)
        recovery = (_task_state(durable).get("runtime_recovery") or {})
        episode = deepcopy(dict((recovery.get("episodes") or {}).get(str(requested.get("episode_id") or "")) or {}))
        # These are durable protocol fields, not a configuration fallback.
        # Corruption must not synthesize a fresh budget or omit its deadline.
        if set(episode["policy"]) != set(asdict(self.policy)):
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_EPISODE_POLICY_INVALID")
        RuntimeRecoveryPolicy(**episode["policy"])
        for field in ("actions_used", "inspections_used", "transient_retries_used", "llm_decisions_used", "explanation_calls_used"):
            if type(episode[field]) is not int or episode[field] < 0:
                raise RuntimeRecoveryError("RUNTIME_RECOVERY_EPISODE_COUNTER_INVALID")
        if isinstance(episode["elapsed_seconds"], bool) or not isinstance(episode["elapsed_seconds"], (int, float)) or not math.isfinite(episode["elapsed_seconds"]) or episode["elapsed_seconds"] < 0:
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_EPISODE_COUNTER_INVALID")
        _time(episode["started_at"])
        stored = episode.get("request") or {}
        if not stored or digest({key: value for key, value in stored.items() if key != "content_digest"}) != stored.get("content_digest") or stored != dict(requested):
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_REQUEST_IDENTITY_INVALID")
        if stored.get("producer") != _PRODUCER or stored.get("execution_state") != "not_dispatched" or stored.get("scope", {}).get("task_id") != task_id:
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_REQUEST_SCOPE_INVALID")
        if recovery.get("active_episode_id") != episode.get("episode_id"):
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_REQUEST_SUPERSEDED")
        observed_scope = self._scope(durable, stored["scope"]["selected_skill_id"], stored["capability_contract"])
        if observed_scope != stored["scope"]:
            raise RuntimeRecoveryError("RUNTIME_RECOVERY_SCOPE_CHANGED")
        if self.inspection_contract_provider:
            observed_contract = self._inspection_contract(stored["scope"]["selected_skill_id"], stored["scope"]["subject"])
            if observed_contract != stored["inspection_contract"]:
                raise RuntimeRecoveryError("RUNTIME_RECOVERY_INSPECTION_CONTRACT_CHANGED")
        return durable, episode, deepcopy(stored)

    def _elapsed_seconds(self, episode: Mapping[str, Any]) -> float:
        """The original durable start prevents process restarts extending time."""
        wall_elapsed = max(0.0, (self._now() - _time(episode["started_at"])).total_seconds())
        return max(float(episode["elapsed_seconds"]), wall_elapsed)

    def _write_episode(self, durable: dict[str, Any], episode: Mapping[str, Any]) -> dict[str, Any]:
        task = _task_state(durable)
        recovery = deepcopy(dict(task.get("runtime_recovery") or {}))
        recovery.setdefault("episodes", {})[episode["episode_id"]] = deepcopy(dict(episode))
        task["runtime_recovery"] = recovery
        durable["task_state"] = task
        save_plan_state(self.data_path, durable["task_id"], durable)
        return load_plan_state(self.data_path, durable["task_id"])

    def _summary(self, durable: Mapping[str, Any], episode: Mapping[str, Any], status: str, code: str) -> dict[str, Any]:
        completed, unfinished = [], []
        for step in durable.get("steps", []) or []:
            if not isinstance(step, Mapping):
                continue
            row = {"step_id": str(step.get("step_id") or ""), "title": str(step.get("title") or ""), "status": str(step.get("status") or "")}
            (completed if row["status"] in {"done", "passed", "completed"} else unfinished).append(row)
        deliveries = []
        for artifact in ((durable.get("provenance_state") or {}).get("artifacts") or {}).values():
            if isinstance(artifact, Mapping) and (artifact.get("verified") is True or str(artifact.get("validation_status") or "").lower() in {"verified", "passed"}):
                path = str(artifact.get("path") or "")
                if path and not re.search(r"(?i)(?:\.env|credential|password|api[_-]?key)", path):
                    deliveries.append({"path": path, "artifact_id": str(artifact.get("artifact_id") or "")})
        attempts = [{"method": str(row.get("method_id") or ""), "inspection_status": str(row.get("inspection_status") or row.get("status") or ""), "reason_code": str(row.get("reason_code") or ""), "progress": bool(row.get("progress"))} for row in episode.get("receipts", []) or []]
        sentences = {
            "evidence_ready": "运行证据已持久化并通过校验，将重新进入当前步骤的生成与审批检查。",
            "dependency_blocked": "声明范围内的有效检查已确认所需运行条件不满足，当前依赖步骤尚未执行。",
            "needs_user_decision": "当前步骤尚未执行；继续需要明确的运行环境选择或授权。",
            "unresolved": "运行证据仍不足，已停止本次自动恢复；这不能证明依赖不存在。",
            "internal_error": "内部运行证据协议或存储出现问题，本次恢复没有批准新的科学执行。",
        }
        if code == "RUNTIME_USER_ANSWER_WITHOUT_SCOPE_CHANGE":
            sentences["unresolved"] = "已收到本次问题的答复，但运行证据或有效授权尚未发生变化；已停止恢复，不会重复提问，也不能据此确认依赖不存在。"
        current_step_id = str(durable.get("current_step_id") or "")
        execution_status = "not_started" if _not_dispatched(durable, current_step_id) else "existing_execution_record"
        return {"execution_status": execution_status, "resolution_disposition": status,
                "failure_class": episode.get("failure_class") or "internal_protocol",
                "original_error_code": (episode.get("request") or {}).get("original_error_code") or code,
                "reason_code": code, "completed_stages": completed, "unfinished_stages": unfinished,
                "verified_deliverables": deliveries, "attempted_recovery": attempts,
                "user_summary": sentences[status] + " 原因：" + code + "。",
                "requires_user_input": status == "needs_user_decision"}

    def _result(self, durable: Mapping[str, Any], episode: Mapping[str, Any], status: str, code: str, assessment: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {"status": status, "request": deepcopy(dict(episode.get("request") or {})),
                "episode": deepcopy(dict(episode)), "reason_code": code,
                "runtime_audit": deepcopy(list((assessment or {}).get("runtime_audit") or [])) if status == "evidence_ready" else [],
                "per_binding_status": deepcopy(dict((assessment or {}).get("facts") or {})),
                "explanation": self._summary(durable, episode, status, code)}

    def _finish(self, durable: dict[str, Any], episode: dict[str, Any], status: str, code: str, assessment: Mapping[str, Any], failure_class: str) -> dict[str, Any]:
        episode.update(status=status, reason_code=code, failure_class=failure_class,
                       last_evidence_semantic_digest=assessment["evidence_semantic_digest"],
                       last_consumer_projection_digest=assessment["consumer_projection_digest"],
                       finished_at=_stamp(self._now()))
        if status == "evidence_ready":
            task = _task_state(durable)
            diagnostic = task.get("runtime_recovery_result")
            if isinstance(diagnostic, Mapping) and diagnostic.get("episode_id") == episode["episode_id"]:
                diagnostics = deepcopy(dict(task.get("runtime_recovery_diagnostics") or {}))
                diagnostics[episode["episode_id"]] = deepcopy(dict(diagnostic))
                task["runtime_recovery_diagnostics"] = diagnostics
                task.pop("runtime_recovery_result", None)
                durable["task_state"] = task
        committed = self._write_episode(durable, episode)
        # Verify the acceptance again from reloaded durable truth. Nothing in a
        # temporary inspector response is an execution authorization object.
        if status == "evidence_ready":
            current_scope = self._scope(committed, episode["request"]["scope"]["selected_skill_id"], episode["request"]["capability_contract"])
            if current_scope != episode["request"]["scope"]:
                raise RuntimeRecoveryError("RUNTIME_RECOVERY_SCOPE_CHANGED")
            fresh = self._assessment(committed, episode["request"]["scope"], episode["request"]["capability_contract"])
            if not fresh["ready"]:
                raise RuntimeRecoveryError("RUNTIME_RECOVERY_ACCEPTANCE_NOT_DURABLE")
            assessment = fresh
        return self._result(committed, episode, status, code, assessment)

    def _records(self, observation: Mapping[str, Any], request: Mapping[str, Any]) -> list[dict[str, Any]]:
        status = str(observation.get("inspection_status") or "output_invalid")
        if status not in {"not_run", "succeeded", "timeout", "permission_error", "execution_error", "output_invalid", "cancelled"}:
            raise RuntimeRecoveryError("RUNTIME_INSPECTION_RESULT_INVALID")
        rows = observation.get("records")
        if rows is not None:
            if not isinstance(rows, list):
                raise RuntimeRecoveryError("RUNTIME_INSPECTION_RECORDS_INVALID")
            records = [validate_runtime_evidence(row) for row in rows]
            for record in records:
                if record["scope"] != request["scope"]["subject"] or record["origin_task_id"] != request["scope"]["task_id"] or record["producer"] not in {"runtime_inspection", "runtime_cache_adoption"} or not set(record["facts"]) <= set(request["inspection_contract"].get("coverage_binding_ids") or []):
                    raise RuntimeRecoveryError("RUNTIME_INSPECTION_RECORD_SCOPE_INVALID")
                if record["producer"] == "runtime_cache_adoption" and not request["inspection_contract"].get("cache_restore_permitted"):
                    raise RuntimeRecoveryError("RUNTIME_INSPECTION_CACHE_ADOPTION_NOT_PERMITTED")
                if status != "succeeded" and any(fact["availability"] != "UNKNOWN" for fact in record["facts"].values()):
                    raise RuntimeRecoveryError("RUNTIME_INSPECTION_RESULT_CONTRADICTION")
            return records
        if "audits" in observation:
            audits = observation["audits"]
            if not isinstance(audits, list) or len(audits) != 1:
                raise RuntimeRecoveryError("RUNTIME_INSPECTION_AUDIT_SCOPE_INVALID")
            observation = {**observation, "audit": audits[0]}
        audit = observation.get("audit")
        if not isinstance(audit, Mapping):
            if status == "succeeded":
                raise RuntimeRecoveryError("RUNTIME_INSPECTION_OUTPUT_INCOMPLETE")
            return []
        observed_at = str(observation.get("observed_at") or "")
        if not observed_at:
            raise RuntimeRecoveryError("RUNTIME_INSPECTION_OBSERVATION_TIME_MISSING")
        coverage = observation.get("coverage")
        if not isinstance(coverage, Mapping):
            raise RuntimeRecoveryError("RUNTIME_INSPECTION_COVERAGE_MISSING")
        permitted = set(request["inspection_contract"].get("coverage_binding_ids") or [])
        if not set(coverage) <= permitted:
            raise RuntimeRecoveryError("RUNTIME_INSPECTION_COVERAGE_OUT_OF_SCOPE")
        return [make_runtime_evidence(
            audit, task_id=request["scope"]["task_id"], skill_scope=request["scope"]["subject"],
            observed_at=observed_at, producer="runtime_inspection",
            source_ref=str(observation.get("source_ref") or "inspection:" + request["inspection_contract"]["contract_id"]),
            coverage=coverage, inspection_status=status,
            supersedes=list(observation.get("supersedes") or []),
        )]

    async def recover(self, state: Mapping[str, Any], pending_request: Mapping[str, Any]) -> dict[str, Any]:
        key = (self.data_path, str(state.get("task_id") or ""), str(pending_request.get("episode_id") or ""))
        lock = _LOCKS.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                return await self._recover(state, pending_request)
            except (RuntimeRecoveryError, RuntimeEvidenceError, ActionRuntimeContractError, StateCASConflict, OSError, ValueError, TypeError, KeyError) as exc:
                code = _code(getattr(exc, "code", None), "RUNTIME_RECOVERY_EPISODE_FIELD_MISSING" if isinstance(exc, KeyError) else "RUNTIME_RECOVERY_STORAGE_OR_PROTOCOL_FAILURE")
                try:
                    durable = load_plan_state(self.data_path, str(state.get("task_id") or ""))
                except Exception:
                    durable = {}
                stored = ((durable.get("task_state") or {}).get("runtime_recovery") or {}).get("episodes") or {}
                episode = deepcopy(dict(stored.get(str(pending_request.get("episode_id") or "")) or {}))
                episode.update(request=deepcopy(dict(pending_request)), failure_class="internal_protocol")
                episode.setdefault("receipts", [])
                if code == "RUNTIME_RECOVERY_CANCELLED":
                    episode["failure_class"] = "cancelled"
                    return self._result(durable, episode, "unresolved", code)
                return self._result(durable, episode, "internal_error", code)

    async def _recover(self, state: Mapping[str, Any], pending: Mapping[str, Any]) -> dict[str, Any]:
        durable, episode, request = self._load_request(state, pending)
        assessment = self._assessment(durable, request["scope"], request["capability_contract"])
        if any(assessment["facts"][binding]["availability"] == "AMBIGUOUS" for binding in request["required_binding_ids"]):
            return self._finish(durable, episode, "internal_error", "RUNTIME_EVIDENCE_CONFLICT", assessment, "internal_protocol")
        if assessment["ready"]:
            diagnostic = _task_state(durable).get("runtime_recovery_result") or {}
            if episode.get("status") == "evidence_ready" and diagnostic.get("episode_id") != episode["episode_id"]:
                return self._result(durable, episode, "evidence_ready", "RUNTIME_EVIDENCE_ACCEPTED", assessment)
            return self._finish(durable, episode, "evidence_ready", "RUNTIME_EVIDENCE_ACCEPTED", assessment, "recovered_runtime_evidence")
        if any(assessment["facts"][binding]["availability"] == "UNAVAILABLE" for binding in request["required_binding_ids"]):
            return self._finish(durable, episode, "dependency_blocked", "REQUIRED_RUNTIME_FACT_CONFIRMED_UNAVAILABLE", assessment, "dependency_unavailable")
        if episode.get("status") == "needs_user_decision":
            # The existing AskUser node owns immutable answer persistence.
            # An answer itself neither grants authorization nor resets recovery.
            conversation = durable.get("conversation_evidence") or {}
            if conversation.get("head_ref"):
                question_id = "runtime-question:" + episode["episode_id"]
                turns = StateContractStore(self.data_path, request["scope"]["task_id"]).read_conversation_evidence(conversation)
                answered = next((turn for turn in turns if turn.get("role") == "user" and turn.get("question_id") == question_id), None)
                if answered is not None:
                    episode["answered_question_id"] = question_id
                    episode["answer_turn_ref"] = str(answered.get("turn_ref") or "")
                    return self._finish(durable, episode, "unresolved", "RUNTIME_USER_ANSWER_WITHOUT_SCOPE_CHANGE", assessment, "authorization_unresolved")
        if episode.get("status") in _TERMINAL and episode.get("last_evidence_semantic_digest") == assessment["evidence_semantic_digest"] and episode.get("last_consumer_projection_digest") == assessment["consumer_projection_digest"]:
            return self._result(durable, episode, episode["status"], episode["reason_code"], assessment)
        inspection = request.get("inspection_contract") or {}
        if not inspection or not set(request["missing_binding_ids"]) <= set(inspection.get("coverage_binding_ids") or []):
            return self._finish(durable, episode, "unresolved", "RUNTIME_INSPECTION_PRODUCER_MISSING", assessment, "internal_configuration")
        if inspection.get("permitted") is not True:
            return self._finish(durable, episode, "unresolved", inspection.get("reason_code") or "RUNTIME_INSPECTION_SAFETY_UNPROVEN", assessment, "inspection_authorization_unproven")
        while True:
            task_budget = (_task_state(durable).get("budgets") or {}).get("runtime_evidence_actions_remaining")
            if task_budget is not None and int(task_budget) <= 0:
                return self._finish(durable, episode, "unresolved", "TASK_RUNTIME_RECOVERY_BUDGET_EXHAUSTED", assessment, episode.get("failure_class") or "runtime_evidence_missing")
            policy = episode["policy"]
            episode["elapsed_seconds"] = self._elapsed_seconds(episode)
            if episode["elapsed_seconds"] >= policy["episode_deadline_seconds"]:
                episode["termination_reason"] = "runtime_recovery_deadline_exceeded"
                last_reason = (episode.get("receipts") or [{}])[-1].get("reason_code") or "RUNTIME_RECOVERY_DEADLINE_EXCEEDED"
                return self._finish(durable, episode, "unresolved", last_reason, assessment, episode.get("failure_class") or "runtime_evidence_missing")
            if episode["actions_used"] >= policy["max_actions"] or episode["inspections_used"] >= policy["max_inspections"] or episode["elapsed_seconds"] >= policy["episode_deadline_seconds"]:
                episode["termination_reason"] = "runtime_recovery_budget_exhausted"
                last_reason = (episode.get("receipts") or [{}])[-1].get("reason_code") or "RUNTIME_RECOVERY_BUDGET_EXHAUSTED"
                return self._finish(durable, episode, "unresolved", last_reason, assessment, episode.get("failure_class") or "runtime_evidence_missing")
            prior = (episode.get("receipts") or [{}])[-1]
            replay = False
            if prior.get("status") == "started":
                if self._now() < _time(prior["lease_until"]):
                    return self._result(durable, episode, "unresolved", "RUNTIME_INSPECTION_ALREADY_RUNNING", assessment)
                replay = True
                if not inspection["safe_repeat"] or episode["transient_retries_used"] >= policy["max_transient_retries"]:
                    return self._finish(durable, episode, "unresolved", "RUNTIME_INSPECTION_COMPLETION_UNKNOWN", assessment, "inspection_error")
                unknown_elapsed = max(0.0, (_time(prior["lease_until"]) - _time(prior["started_at"])).total_seconds())
                prior.update(status="interrupted_unknown", inspection_status="completion_unknown", reason_code="RUNTIME_INSPECTION_COMPLETION_UNKNOWN", elapsed_seconds=unknown_elapsed)
                episode["elapsed_seconds"] += unknown_elapsed
                if episode["elapsed_seconds"] >= policy["episode_deadline_seconds"]:
                    return self._finish(durable, episode, "unresolved", "RUNTIME_INSPECTION_COMPLETION_UNKNOWN", assessment, "inspection_error")
            elif prior.get("status") == "completed":
                transient = prior.get("transient") is True and prior.get("inspection_status") in {"timeout", "execution_error"}
                if not (transient and inspection["safe_repeat"] and episode["transient_retries_used"] < policy["max_transient_retries"]):
                    reason = prior.get("reason_code") or "RUNTIME_INSPECTION_NO_PROGRESS"
                    return self._finish(durable, episode, "unresolved", reason, assessment, episode.get("failure_class") or "inspection_error")
                replay = True
            remaining = policy["episode_deadline_seconds"] - episode["elapsed_seconds"]
            deadline = min(inspection["deadline_seconds"], policy["inspection_deadline_seconds"], remaining)
            receipt = {
                "receipt_id": "runtime-inspection:" + digest({"episode": episode["episode_id"], "ordinal": episode["inspections_used"] + 1}),
                "method_id": inspection["contract_id"], "method_digest": inspection["contract_digest"],
                "input_signature": digest({"scope": request["scope"], "method": inspection["contract_digest"], "projection": assessment["consumer_projection_digest"]}),
                "status": "started", "owner_id": self.owner_id,
                "started_at": _stamp(self._now()), "lease_until": _stamp(self._now() + timedelta(seconds=deadline)),
                "safe_repeat": inspection["safe_repeat"], "replay": replay,
            }
            episode["receipts"].append(receipt)
            episode["actions_used"] += 1
            episode["inspections_used"] += 1
            episode["transient_retries_used"] += int(replay)
            episode["status"] = "inspecting"
            if task_budget is not None:
                durable["task_state"]["budgets"]["runtime_evidence_actions_remaining"] = int(task_budget) - 1
            durable = self._write_episode(durable, episode)  # durable dispatch receipt first
            inspection_input = deepcopy(request)
            inspection_input.update(inspection_receipt_id=receipt["receipt_id"], inspection_deadline_seconds=deadline, operation="inspect")
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(self.inspect_runtime(inspection_input), timeout=deadline)
                observation = dict(response) if isinstance(response, Mapping) else {"inspection_status": "output_invalid", "reason_code": "RUNTIME_INSPECTION_OUTPUT_INVALID"}
            except asyncio.TimeoutError:
                observation = {"inspection_status": "timeout", "reason_code": "RUNTIME_INSPECTION_TIMEOUT", "transient": True}
            except (asyncio.CancelledError, GraphBubbleUp):
                raise
            except Exception:
                observation = {"inspection_status": "execution_error", "reason_code": "RUNTIME_INSPECTOR_EXCEPTION", "transient": False}
            elapsed = max(0.0, time.monotonic() - started)
            # Reload and compare relevant scope before accepting late results.
            durable, episode, request = self._load_request(state, pending)
            current = episode["receipts"][-1]
            if current["receipt_id"] != receipt["receipt_id"] or current["status"] != "started":
                raise RuntimeRecoveryError("RUNTIME_INSPECTION_RECEIPT_CONFLICT")
            inspection_status = str(observation.get("inspection_status") or "output_invalid")
            error_code = _code(observation.get("reason_code"), "RUNTIME_INSPECTION_OUTPUT_INCOMPLETE" if inspection_status == "succeeded" else "RUNTIME_INSPECTION_" + inspection_status.upper())
            integrity_failure = False
            try:
                records = self._records(observation, request)
                if records:
                    if inspection_status == "succeeded" and not observation.get("reason_code"):
                        error_code = "RUNTIME_INSPECTION_COMPLETED"
                    task = _task_state(durable)
                    prior_evidence = dict(task.get("runtime_evidence") or {})
                    accepted = merge_runtime_evidence(prior_evidence.get("records") or [], records, task_id=request["scope"]["task_id"], accepted_at=_stamp(self._now()))
                    task["runtime_evidence"] = runtime_evidence_state(accepted, prior_revision=prior_evidence.get("revision", 0))
                    durable["task_state"] = task
            except (RuntimeRecoveryError, RuntimeEvidenceError) as exc:
                records = []
                inspection_status = "output_invalid"
                error_code = _code(exc.code, "RUNTIME_INSPECTION_OUTPUT_INVALID")
                integrity_failure = isinstance(exc, RuntimeEvidenceError) or any(part in error_code for part in ("SCOPE", "CONTRADICTION", "IDENTITY", "ADOPTION_NOT_PERMITTED"))
            refreshed = self._assessment(durable, request["scope"], request["capability_contract"])
            progress = refreshed["evidence_semantic_digest"] != assessment["evidence_semantic_digest"] or refreshed["consumer_projection_digest"] != assessment["consumer_projection_digest"]
            current.update(status="completed", inspection_status=inspection_status,
                           finished_at=_stamp(self._now()), elapsed_seconds=elapsed,
                           reason_code=error_code, transient=bool(observation.get("transient")) and inspection_status in {"timeout", "execution_error"},
                           evidence_ids=[record["evidence_id"] for record in records], progress=progress)
            episode["elapsed_seconds"] += elapsed
            episode["failure_class"] = "inspection_error" if inspection_status != "succeeded" else "runtime_evidence_missing"
            episode["last_evidence_semantic_digest"] = refreshed["evidence_semantic_digest"]
            episode["last_consumer_projection_digest"] = refreshed["consumer_projection_digest"]
            durable = self._write_episode(durable, episode)  # acceptance and completed result atomic
            assessment = self._assessment(durable, request["scope"], request["capability_contract"])
            if integrity_failure:
                return self._finish(durable, episode, "internal_error", error_code, assessment, "internal_protocol")
            if any(assessment["facts"][binding]["availability"] == "AMBIGUOUS" for binding in request["required_binding_ids"]):
                return self._finish(durable, episode, "internal_error", "RUNTIME_EVIDENCE_CONFLICT", assessment, "internal_protocol")
            if assessment["ready"]:
                return self._finish(durable, episode, "evidence_ready", "RUNTIME_EVIDENCE_ACCEPTED", assessment, "recovered_runtime_evidence")
            if any(assessment["facts"][binding]["availability"] == "UNAVAILABLE" for binding in request["required_binding_ids"]):
                return self._finish(durable, episode, "dependency_blocked", "REQUIRED_RUNTIME_FACT_CONFIRMED_UNAVAILABLE", assessment, "dependency_unavailable")
            if observation.get("needs_user_decision"):
                # A decision can only select/authorize a declared blocked scope.
                # It never installs anything or treats waiting as permission.
                decision = observation.get("needs_user_decision") or {}
                question = str(decision.get("question") or "").strip()
                if not question or len(question) > 1000 or re.search(r"(?i)(?:password|credential|api[_-]?key|\.env)", question):
                    return self._finish(durable, episode, "unresolved", "RUNTIME_USER_DECISION_NOT_ACTIONABLE", assessment, "internal_configuration")
                episode["user_decision"] = {"kind": _code(decision.get("kind"), "runtime_authorization_required"), "contract_id": inspection["contract_id"], "question": question}
                return self._finish(durable, episode, "needs_user_decision", "RUNTIME_USER_DECISION_REQUIRED", assessment, "authorization_required")
