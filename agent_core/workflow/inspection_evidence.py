"""Episode-scoped inspection snapshots over the existing StateContractStore.

Objects are immutable; their handles are published with the causal packet in
PlanTracker's CAS commit.  This module owns neither routing nor step completion.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from agent_core.state_contract_store import StateContractStore, StateContractIntegrityError

INSPECTION_BACKEND = "state_contract_inspection"
INSPECTION_OBJECT_KINDS = frozenset({
    "validation", "completion_report", "semantic_decision", "step_attempt", "execution",
    "action", "script", "task_context", "plan", "diagnostic", "parser_receipt", "causal_history", "generation_context", "failure_log",
})
INSPECTION_SCOPE_FIELDS = (
    "task_id", "plan_id", "plan_version", "step_id", "attempt_id", "episode_id",
)
MAX_INSPECTION_OBJECT_BYTES = 2 * 1024 * 1024
MAX_INSPECTION_AUTHORITY_BYTES = 16 * 1024 * 1024
MAX_INSPECTION_READ_LINES = 120
MAX_INSPECTION_READ_CHARS = 16_000


class InspectionEvidenceError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code, self.detail = code, detail
        super().__init__(code + (": " + detail if detail else ""))


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def inspection_scope(packet: Any, episode_id: str) -> dict[str, str]:
    return {
        "task_id": packet.task_id,
        "plan_id": packet.plan_spec_ref.plan_id,
        "plan_version": packet.plan_spec_ref.plan_version,
        "step_id": packet.step_id,
        "attempt_id": packet.attempt_ref,
        "episode_id": str(episode_id),
    }


def _object_type(kind: str) -> str:
    if kind not in INSPECTION_OBJECT_KINDS:
        raise InspectionEvidenceError("INSPECTION_OBJECT_TYPE_INVALID", kind)
    return "recovery_inspection." + kind


def _handle(scope: Mapping[str, Any], kind: str, source_id: str, digest: str = "") -> str:
    return "inspection:sha256:" + hashlib.sha256(_canonical({
        "scope": dict(scope), "kind": kind, "source_object_id": source_id,
        "digest": digest,
    }).encode()).hexdigest()


def _safe_error_detail(exc: Exception) -> str:
    text = str(getattr(exc, "detail", "") or exc)
    for value in (getattr(exc, "filename", None), getattr(exc, "filename2", None)):
        if value:
            text = text.replace(str(value), "[path]")
    # Store errors may embed an absolute path in their message rather than the
    # OSError filename attributes.  The model needs errno/reason, not locators.
    text = re.sub(r"(?<![A-Za-z0-9])/(?:[^\s'\";:,)]*)", "[path]", text)
    return text[:400]


def _assert_storage_path(store: StateContractStore, leaf: Path | None = None) -> None:
    path = leaf or store.object_root
    # Check ancestors before descendants so a symlink above data_path is also
    # denied without opening a byte of the target object.
    for component in reversed((path, *path.parents)):
        if component.is_symlink():
            raise InspectionEvidenceError("INSPECTION_OBJECT_PATH_DENIED")


def read_execution_facts(store: StateContractStore, reference: str) -> dict[str, Any]:
    """Bounded immutable-store read; the caller must still check attempt scope."""
    from agent_core.approved_resource_locator import open_approved_resource
    digest = store._parse_ref(reference, "object")
    path = store.object_root / (digest + ".json")
    _assert_storage_path(store, path)
    with open_approved_resource(path) as stream:
        encoded = stream.read(MAX_INSPECTION_OBJECT_BYTES + 1)
    if len(encoded) > MAX_INSPECTION_OBJECT_BYTES:
        raise InspectionEvidenceError("INSPECTION_OBJECT_SIZE_LIMIT")
    record = json.loads(encoded)
    if hashlib.sha256(_canonical(record).encode()).hexdigest() != digest:
        raise InspectionEvidenceError("INSPECTION_OBJECT_INTEGRITY_ERROR")
    if (not isinstance(record, Mapping) or type(record.get("schema_version")) is not int
            or record["schema_version"] != 1 or record.get("object_type") != "execution.recovery_facts"
            or not isinstance(record.get("payload"), Mapping)):
        raise InspectionEvidenceError("INSPECTION_OBJECT_SCHEMA_INVALID")
    return dict(record["payload"])


def _assert_body_scope(kind: str, body: Mapping[str, Any], scope: Mapping[str, Any]) -> None:
    if type(body.get("schema_version")) is not int or body["schema_version"] != 1:
        raise InspectionEvidenceError("INSPECTION_OBJECT_SCHEMA_INVALID", kind)
    actual = body if kind == "validation" else body.get("scope")
    if kind == "step_attempt":
        actual = body.get("scope")
    if not isinstance(actual, Mapping):
        raise InspectionEvidenceError("INSPECTION_OBJECT_SCOPE_MISSING", kind)
    fields = ("task_id", "step_id", "attempt_id") if kind == "validation" else (
        "task_id", "plan_id", "plan_version", "step_id", "attempt_id",
    )
    for field in fields:
        if str(actual.get(field) or "") != str(scope.get(field) or ""):
            raise InspectionEvidenceError("INSPECTION_OBJECT_SCOPE_MISMATCH", kind + ":" + field)


def assert_current_inspection_scope(
    durable: Mapping[str, Any], scope: Mapping[str, Any], *, require_episode: bool = True,
) -> Mapping[str, Any]:
    """Authorize against current durable identity, never a cached generation.

    The producer checks before publication; readers additionally require the
    published active episode. A retained packet cannot authorize a new attempt.
    """
    if any(not str(scope.get(key) or "") for key in INSPECTION_SCOPE_FIELDS if key != "attempt_id"):
        raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISSING")
    for field, value in (
        ("task_id", durable.get("task_id")), ("plan_id", durable.get("plan_id")),
        ("plan_version", durable.get("plan_version")),
        ("step_id", durable.get("current_step_id")),
    ):
        if str(value or "") != str(scope[field]):
            raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISMATCH", field)
    raw_steps = durable.get("steps") or []
    rows = raw_steps.values() if isinstance(raw_steps, Mapping) else raw_steps
    step = next((row for row in rows if isinstance(row, Mapping)
                 and str(row.get("step_id") or "") == scope["step_id"]), {})
    task = dict(durable.get("task_state") or {})
    pending = dict(task.get("verification_pending") or {})
    attempts = [str(value) for value in (step.get("verification_attempt_id"),
                pending.get("attempt_id")) if value]
    active_record = (task.get("causal_failure_packets") or {}).get(task.get("active_failure_packet_ref")) or {}
    attempt_id = str(scope.get("attempt_id") or "")
    if attempt_id:
        attempt = ((durable.get("provenance_state") or {}).get("attempts") or {}).get(attempt_id) or {}
        if not attempt or str(attempt.get("step_id") or "") != scope["step_id"]:
            raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISMATCH", "attempt_id")
        active_attempt = str((active_record.get("failure_packet") or {}).get("attempt_ref") or "")
        if require_episode and active_attempt != attempt_id:
            raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISMATCH", "attempt_id")
        if attempts and any(value != attempt_id for value in attempts):
            recovery = task.get("recovery_state") or {}
            active = recovery.get("active_recovery") or {}
            receipt = (task.get("typed_recovery_receipts") or {}).get(active.get("recovery_id")) or {}
            envelope = receipt.get("execution_envelope") or {}
            if ((receipt.get("directive") or {}).get("action") not in {"run_diagnostic_probe", "retry_step"}
                    or envelope.get("action_id") != attempt.get("action_id")
                    or envelope.get("step_id") != scope["step_id"]
                    or active.get("envelope_id") != receipt.get("execution_envelope_digest")
                    or any(value != attempt_id and value != (envelope.get("source_scope") or {}).get("attempt_id") for value in attempts)):
                raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISMATCH", "attempt_id")
    if (not step or str(step.get("status") or "") in {"done", "passed", "completed", "cancelled", "skipped"}
            or str(task.get("status") or "") in {"completed", "cancelled"}):
        raise InspectionEvidenceError("INSPECTION_EPISODE_INACTIVE")
    if not require_episode:
        return {}
    active = str(task.get("active_failure_packet_ref") or "")
    record = (task.get("causal_failure_packets") or {}).get(active) or {}
    packet = record.get("failure_packet") or {}
    plan = packet.get("plan_spec_ref") or {}
    actual = {
        "task_id": packet.get("task_id"), "plan_id": plan.get("plan_id"),
        "plan_version": plan.get("plan_version"), "step_id": packet.get("step_id"),
        "attempt_id": packet.get("attempt_ref"),
        "episode_id": "recovery-seed-" + str(record.get("source_fingerprint") or ""),
    }
    publication = record.get("inspection_publication") or {}
    if (not active or publication.get("schema_version") != 1
            or publication.get("policy") != "typed_recovery_evidence_v2"
            or publication.get("source_fingerprint") != record.get("source_fingerprint")):
        raise InspectionEvidenceError("INSPECTION_EPISODE_INACTIVE")
    for field in INSPECTION_SCOPE_FIELDS:
        if str(actual.get(field) or "") != str(scope[field]):
            raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISMATCH", field)
    return packet


def prepare_inspection_evidence(
    *, data_path: str, durable: Mapping[str, Any], state: Mapping[str, Any],
    source: Mapping[str, Any], packet: Any, episode_id: str,
) -> dict[str, dict[str, Any]]:
    """Write real producer objects, returning unpublished registry descriptors.

    Failures are explicit unavailable descriptors, so one optional storage
    failure does not remove the remaining lawful inspection capabilities.
    """
    scope = inspection_scope(packet, episode_id)
    assert_current_inspection_scope(durable, scope, require_episode=False)
    payload = dict(source.get("payload") or {})
    task = dict(durable.get("task_state") or {})
    rows = durable.get("steps") or []
    rows = rows.values() if isinstance(rows, Mapping) else rows
    step = next((deepcopy(dict(row)) for row in rows if isinstance(row, Mapping)
                 and str(row.get("step_id") or "") == scope["step_id"]), {})
    plan = dict((durable.get("current_plan") or {}).get("plan_spec") or {})
    contract_step = next((deepcopy(dict(row)) for row in plan.get("steps", [])
                         if isinstance(row, Mapping) and str(row.get("step_id") or "") == scope["step_id"]), {})
    def scoped(**value: Any) -> dict[str, Any]:
        return {"schema_version": 1, "scope": scope, **value}

    # TypedExecute deliberately clears pending_action and exposes the accepted
    # record through ExecuteOutcome. A later Supervisor rejection does not
    # duplicate that record in its own payload. Preserve this real graph seam
    # before falling back to the smaller provenance attempt projection.
    execute_payload = dict((state.get("execute_outcome") or {}).get("payload") or {})
    execution = dict(payload.get("execution_record") or payload.get("diagnostic_record")
        or state.get("execution_evidence") or execute_payload.get("execution_record")
        or execute_payload.get("diagnostic_record") or {})
    if not scope["attempt_id"]:
        # Candidate rejection has no execution. Never inherit an earlier
        # graph state's rc, script or parser receipt to fill that absence.
        execution = {}
    if not execution and scope["attempt_id"]:
        execution = dict(((durable.get("provenance_state") or {}).get("attempts") or {}).get(scope["attempt_id"]) or {})
    execution_error = None
    if execution:
        for field in ("plan_id", "plan_version", "step_id", "attempt_id"):
            if str(execution.get(field) or "") != scope[field]:
                execution_error = InspectionEvidenceError("EXECUTION_SCOPE_MISMATCH", field)
        if execution_error is None and execution.get("recovery_execution_facts_ref"):
            try:
                held = read_execution_facts(StateContractStore(data_path, scope["task_id"]), str(execution["recovery_execution_facts_ref"]))
                for field in ("plan_id", "plan_version", "step_id", "attempt_id", "action_id"):
                    if str(held.get(field) or "") != str(execution.get(field) or ""):
                        raise InspectionEvidenceError("INSPECTION_OBJECT_SCOPE_MISMATCH", field)
                execution = {**execution, **held}
            except (ValueError, TypeError, OSError) as exc:
                execution_error = InspectionEvidenceError(str(getattr(exc, "code", "") or "EXECUTION_DETAILS_UNAVAILABLE"), _safe_error_detail(exc))
    # Publish a selected fact record, never arbitrary environment/configuration
    # or a model-supplied path. Actual script bodies are archived by Execute.
    from agent_core.context_lineage import redact_text
    fact_keys = ("schema_version", "task_id", "plan_id", "plan_version", "step_id", "purpose", "declared_outputs", "produced_artifacts", "declared_step_effect", "step_effect", "execution_id", "attempt_id", "action_id", "returncode", "exit_code", "execution_status",
                 "process_status", "command_role", "code", "raw_stdout", "raw_stderr", "stdout", "stderr",
                 "pipeline_statuses", "validation", "effect_evidence", "timed_out", "duration_seconds")
    facts = {key: deepcopy(execution[key]) for key in fact_keys if key in execution} if execution_error is None else {}
    facts = {key: redact_text(value) if isinstance(value, str) else value for key, value in facts.items()}
    snapshot = scoped(
        approved_step=contract_step,
        plan_approval_status=str(durable.get("approval_status") or "unknown"),
        step_snapshot_unavailable_reason="" if contract_step else "HISTORICAL_STEP_SNAPSHOT_UNAVAILABLE",
        execution_record=facts,
        execution_unavailable_reason=str(execution_error.code) if execution_error else ("" if execution else "NOT_APPLICABLE_NO_ATTEMPT" if not scope["attempt_id"] else "HISTORICAL_BODY_UNAVAILABLE"),
        verification_status=str(step.get("status") or "unknown"), commit_status="not_committed",
    )
    pending = dict(state.get("pending_action") or {})
    if not pending and execution_error is None and scope["attempt_id"]:
        provenance = durable.get("provenance_state") or {}
        attempt = (provenance.get("attempts") or {}).get(scope["attempt_id"]) or {}
        action_id = str(attempt.get("action_id") or "")
        if action_id and action_id == str(execution.get("action_id") or ""):
            accepted = (provenance.get("actions") or {}).get(action_id) or {}
            pending = dict(accepted.get("normalized_action") or {})
    action = {key: deepcopy(pending[key]) for key in (
        "action_id", "step_id", "plan_id", "plan_version", "action_kind", "command_role", "purpose",
        "expected_step_effect", "success_checks", "inputs", "outputs", "generated_script", "approved_script",
        "generation_provenance", "recovery_execution_envelope",
    ) if key in pending}
    if action and (any(str(action.get(key) or "") != scope[key]
                      for key in ("step_id", "plan_id", "plan_version")) or
                   (execution.get("action_id") and action.get("action_id") != execution.get("action_id"))):
        action = {}
    from agent_core.plan_tracker import canonical_task_spec_for_state
    context = scoped(task_spec=canonical_task_spec_for_state(durable),
                     accepted_consultant_handoff=deepcopy(task.get("accepted_consultant_handoff") or {}))
    scripts = {key: redact_text(str(execution[key])) for key in ("code", "generated_code", "execution_wrapper")
               if key in execution and execution_error is None}
    if execution_error is None and execution.get("script_identities"):
        scripts["identities"] = deepcopy(execution["script_identities"])
    if execution_error is None and execution.get("argv"):
        scripts["argv"] = deepcopy(execution["argv"])
    scripts["availability"] = {key: "recorded" if key in scripts else "HISTORICAL_BODY_UNAVAILABLE"
                               for key in ("code", "generated_code", "execution_wrapper")}
    objects = [
        ("validation", str((payload.get("validation_evidence") or {}).get("validation_id") or "validation"), payload.get("validation_evidence")),
        ("completion_report", str((payload.get("completion_report") or {}).get("report_id") or "completion_report"), payload.get("completion_report")),
        ("semantic_decision", str((payload.get("semantic_decision") or {}).get("decision_id") or "semantic_decision"), payload.get("semantic_decision")),
        ("step_attempt", scope["step_id"] + ":" + scope["attempt_id"], snapshot),
        ("execution", scope["attempt_id"] or "not_executed", scoped(execution={k: v for k, v in facts.items() if k in {"execution_id", "attempt_id", "action_id", "returncode", "exit_code", "execution_status", "code", "raw_stdout", "raw_stderr", "stdout", "stderr"}}, execution_status="recorded" if execution else "not_executed",
            claim_status="script assertions are not independently verified")),
        ("script", scope["attempt_id"] or "not_executed", scoped(scripts=scripts)),
        ("action", str(action.get("action_id") or "unavailable"), scoped(action=action, unavailable_reason="" if action else "HISTORICAL_BODY_UNAVAILABLE")),
        ("task_context", str(packet.task_spec_ref.task_revision) if hasattr(packet, "task_spec_ref") else "task", context),
        ("plan", scope["plan_id"], scoped(approved_plan=plan)),
        ("parser_receipt", scope["attempt_id"] or "not_executed", scoped(parser_receipt=(facts.get("effect_evidence") or {}).get("parser_receipt") or {},
            availability="recorded" if (facts.get("effect_evidence") or {}).get("parser_receipt") else "HISTORICAL_BODY_UNAVAILABLE")),
    ]
    if execution_error is None and scope["attempt_id"]:
        for log in execution.get("failure_logs", []) or []:
            if (log.get("source") != "runtime_attester_snapshot"
                    or any(str(log.get(key) or "") != scope[key]
                           for key in ("task_id", "plan_id", "step_id", "attempt_id"))
                    or log.get("action_id") != execution.get("action_id")):
                continue
            log_body = deepcopy(log)
            chunks = log_body.pop("text_chunks", [])
            text = "".join(chunks)
            # Stored content is already bounded/redacted by the runtime. The
            # registry offers only evidence.read, retaining all scope guards.
            if log.get("status") == "captured" and ("sha256:" + hashlib.sha256(text.encode()).hexdigest()) != log.get("text_sha256"):
                continue
            log_body["text_lines"] = [line[i:i + 2000] for line in text.splitlines(keepends=True)
                                      for i in range(0, len(line), 2000)]
            objects.append(("failure_log", str(log.get("relative_path")), scoped(log=log_body,
                read_instructions="Use evidence.read with this ref_id. If truncated, repeat evidence.read on the same ref with start_line=cursor.next_start_line; no step.inspect or evidence.glob. Log publication is not step completion.")))
    # Current Generate's semantic input/candidate/feedback can be inspected on
    # demand. Reuse its immutable message snapshots; do not copy System prompts
    # or claim that a reconstructed episode is an exact historical wire call.
    raw_episode = task.get("generate_context_episode")
    if isinstance(raw_episode, Mapping) and raw_episode:
        try:
            from agent_core.workflow.generate_handoff import GenerateReasoningEpisode
            episode = GenerateReasoningEpisode.from_mapping(raw_episode)
            basis = episode.basis
            from agent_core.plan_tracker import load_approved_plan_snapshot, semantic_plan_hash
            from agent_core.task_spec import task_spec_revision, task_semantic_hash
            approved = load_approved_plan_snapshot(data_path, scope["task_id"], state=durable)
            task_spec = canonical_task_spec_for_state(durable)
            expected_task = str(task_spec_revision(task_spec)) + ":" + task_semantic_hash(task_spec)
            expected_plan = str(durable.get("plan_revision") or 0) + ":sha256:" + semantic_plan_hash(approved or durable)
            selected_refs = (task.get("skill_access_state") or durable.get("skill_access_state") or {}).get("selected_skill_refs") or []
            selected_revision = "sha256:" + hashlib.sha256(_canonical(selected_refs).encode()).hexdigest()
            if (basis.task_id != scope["task_id"] or basis.plan_id != scope["plan_id"]
                    or basis.step_id != scope["step_id"]
                    or basis.plan_revision != expected_plan
                    or basis.task_revision != expected_task
                    or basis.selected_skill_revision != selected_revision):
                raise InspectionEvidenceError("GENERATE_CONTEXT_SCOPE_MISMATCH", "current invocation basis")
            def readable_message(message):
                # Preserve exact content with reversible bounded chunks, so a
                # long escaped JSON string does not exceed the line reader's
                # budget and become impossible to paginate.
                value = deepcopy(message.to_dict())
                content = value["payload"].pop("content", "")
                text = _canonical(content)
                value["content_representation"] = "join chunks then JSON decode"
                value["content_json_sha256"] = hashlib.sha256(text.encode()).hexdigest()
                value["content_chunks"] = [text[i:i + 2000] for i in range(0, len(text), 2000)]
                return value
            context_body = scoped(
                representation="durable Generate episode semantic messages",
                coverage={"system_prompt": "not_exposed", "provider_wire": "not_reconstructed",
                    "earlier_epochs": "see original archive reference"},
                reasoning_episode_id=episode.reasoning_episode_id,
                context_epoch=episode.context_epoch,
                previous_context_archive_ref=episode.previous_context_archive_ref,
                seed=readable_message(episode.seed_message),
                inherited_materials=[readable_message(item) for item in episode.inherited_materials
                    if item.payload.get("type") != "system"],
                candidate_and_feedback=[{"event_id": item.event_id, "event_kind": item.event_kind,
                    "message": readable_message(item.message)} for item in episode.local_events
                    if item.message.payload.get("type") != "system"],
            )
        except (ValueError, TypeError) as exc:
            context_body = scoped(availability="UNAVAILABLE",
                unavailable_reason=str(getattr(exc, "code", "") or "GENERATE_CONTEXT_INVALID"))
        objects.append(("generation_context", str(raw_episode.get("reasoning_episode_id") or "current"), context_body))
    if payload.get("diagnostic_record"):
        objects.append(("diagnostic", scope["attempt_id"], scoped(diagnostic_record=deepcopy(payload["diagnostic_record"]))))
    parent_ref = str(payload.get("causal_parent_packet_ref") or "")
    decision = str(payload.get("recovery_decision_id") or "")
    retry_parent = False
    if not parent_ref and packet.action_ref:
        # Ordinary retry outcomes do not carry the diagnostic-probe lineage
        # fields. Resolve only their exact adopted execution envelope, never
        # a previous successful attempt or an arbitrary same-step packet.
        active = (task.get("recovery_state") or {}).get("active_recovery") or {}
        decision = str(active.get("recovery_id") or "")
        receipt = (task.get("typed_recovery_receipts") or {}).get(decision) or {}
        envelope = receipt.get("execution_envelope") or {}
        candidate_ref = str(receipt.get("failure_packet_ref") or "")
        candidate = ((task.get("causal_failure_packets") or {}).get(candidate_ref) or {}).get("failure_packet") or {}
        origin = envelope.get("source_scope") or {}
        if ((receipt.get("directive") or {}).get("action") == "retry_step"
                and envelope.get("action_id") == packet.action_ref
                and envelope.get("step_id") == packet.step_id
                and active.get("envelope_id") == receipt.get("execution_envelope_digest")
                and candidate.get("task_id") == packet.task_id
                and candidate.get("step_id") == packet.step_id
                and candidate.get("attempt_ref") == origin.get("attempt_id")
                and candidate_ref):
            parent_ref, retry_parent = candidate_ref, True
    if parent_ref:
        # Publication is still under the parent CAS. Read only the current
        # registered parent objects, verifying their original hashes/scopes.
        receipt = (task.get("typed_recovery_receipts") or {}).get(decision) or {}
        parent = (task.get("causal_failure_packets") or {}).get(parent_ref) or {}
        parent_incident = (parent.get("failure_packet") or {}).get("incident_id")
        consumed = (task.get("consumed_failure_packet_refs") or {}).get(parent_ref) or {}
        if (str(task.get("active_failure_packet_ref") or "") not in {"", parent_ref}
                or (task.get("causal_failure_incident_heads") or {}).get(parent_incident) != parent_ref
                or consumed.get("decision_id") != decision
                or receipt.get("failure_packet_ref") != parent_ref
                or receipt.get("incident_id") != parent_incident
                or (not retry_parent and parent_incident != packet.incident_id)
                or (receipt.get("directive") or {}).get("action") not in {"run_diagnostic_probe", "retry_step"}):
            raise InspectionEvidenceError("DIAGNOSTIC_PARENT_SCOPE_STALE", parent_ref)
        inherited = []
        for ref in (parent.get("inspection_publication") or {}).get("refs") or ():
            entry = (task.get("inspection_ref_registry") or {}).get(ref) or {}
            kind = (entry.get("scope") or {}).get("object_kind")
            if kind not in {"execution", "script", "validation", "completion_report", "semantic_decision", "parser_receipt", "causal_history", "failure_log"}:
                continue
            row = {"ref_id": ref, "object_kind": kind, "source_digest": entry.get("digest"), "source_scope": entry.get("scope")}
            try:
                reader = InspectionObjectReader(data_path, scope={k: entry["scope"].get(k, "") for k in INSPECTION_SCOPE_FIELDS})
                row["body"] = reader.read_causal_parent_body(entry, entry.get("locator") or {},
                    durable=durable, parent_ref=parent_ref, decision_id=decision)
            except (ValueError, OSError) as exc:
                row["unavailable_reason"] = str(getattr(exc, "code", "") or "PARENT_EVIDENCE_READ_FAILED")
            inherited.append(row)
        objects.append(("causal_history", parent_ref, scoped(
            use="historical_diagnostic_only", formal_evidence_reuse=False,
            original_failure=deepcopy(parent.get("failure_packet") or {}),
            parent_evidence=inherited,
            prior_tool_results=deepcopy(receipt.get("causal_observation_receipts") or []),
            recovery_archive_ref=str(receipt.get("recovery_archive_ref") or ""))))
    store = StateContractStore(data_path, scope["task_id"])
    entries: dict[str, dict[str, Any]] = {}
    for kind, source_id, raw_body in objects:
        body = deepcopy(dict(raw_body)) if isinstance(raw_body, Mapping) else {}
        reason, detail, object_ref = "", "", ""
        try:
            if kind in {"execution", "script", "parser_receipt"} and execution_error is not None:
                raise execution_error
            if not body:
                if kind in {"validation", "completion_report", "semantic_decision"} and "verification" not in str(source.get("type") or "") and str(source.get("type") or "") in {"execution_failed", "pre_execution_rejected", "diagnostic_completed", "install_failed", "installation_failed"}:
                    raise InspectionEvidenceError("NOT_APPLICABLE_NOT_PRODUCED", kind)
                raise InspectionEvidenceError("HISTORICAL_BODY_UNAVAILABLE", kind)
            _assert_body_scope(kind, body, scope)
            wrapper = {
                "schema_version": 1, "object_kind": kind, "object_revision": 1,
                "source_object_id": source_id, "scope": scope,
                "producer": "typed_recovery_source:" + str(source.get("type") or ""), "source_outcome_ref": packet.source_outcome_ref,
                "body": body,
            }
            encoded = _canonical(wrapper).encode("utf-8")
            if len(encoded) > MAX_INSPECTION_OBJECT_BYTES - 1024:
                raise InspectionEvidenceError("INSPECTION_OBJECT_SIZE_LIMIT", kind)
            _assert_storage_path(store)
            object_ref = store.put_object(_object_type(kind), wrapper)
        except (InspectionEvidenceError, StateContractIntegrityError, OSError, ValueError, TypeError) as exc:
            reason = str(getattr(exc, "code", "") or "INSPECTION_OBJECT_PERSIST_FAILED")
            detail = _safe_error_detail(exc)
        digest = object_ref.removeprefix("object:") if object_ref else "sha256:" + hashlib.sha256(_canonical({"scope":scope,"kind":kind,"source_object_id":source_id}).encode()).hexdigest()
        ref = _handle(scope, kind, source_id, digest if object_ref else "")
        entries[ref] = {
            "ref_id": ref, "kind": "evidence", "task_id": scope["task_id"],
            "plan_id": scope["plan_id"], "step_id": scope["step_id"],
            "backend": INSPECTION_BACKEND, "digest": digest,
            "size_hint": len(_canonical(body).encode()) if body else 0,
            "scope": {
                **scope, "access": "current_step", "readable": bool(object_ref),
                "resolvable": bool(object_ref), "object_kind": kind,
                "source_object_id": source_id, "object_revision": 1,
                "allowed_tools": ["evidence.read"], "unavailable_reason": reason,
                "unavailable_detail": detail,
            },
            "locator": {"object_ref": object_ref, "object_type": _object_type(kind)} if object_ref else {},
        }
    return entries


def validate_inspection_descriptor(entry: Mapping[str, Any], expected_scope: Mapping[str, Any]) -> None:
    """Check authorization metadata before opening any object body."""
    if entry.get("backend") != INSPECTION_BACKEND or entry.get("kind") != "evidence":
        raise InspectionEvidenceError("INSPECTION_BACKEND_UNSUPPORTED")
    actual = dict(entry.get("scope") or {})
    for field in INSPECTION_SCOPE_FIELDS:
        top = entry.get(field, actual.get(field))
        if str(actual.get(field) or "") != str(expected_scope.get(field) or "") or str(top or "") != str(expected_scope.get(field) or ""):
            raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISMATCH", field)
    _object_type(str(actual.get("object_kind") or ""))
    if type(actual.get("object_revision")) is not int or actual["object_revision"] != 1:
        raise InspectionEvidenceError("INSPECTION_OBJECT_REVISION_MISMATCH")
    if actual.get("allowed_tools") != ["evidence.read"]:
        raise InspectionEvidenceError("INSPECTION_TOOL_NOT_ALLOWED")


class InspectionObjectReader:
    """Fixed store accessor.  No provider path, remote locator or raw task dump."""
    def __init__(self, data_path: str, *, scope: Mapping[str, Any]) -> None:
        self.scope = dict(scope)
        self.store = StateContractStore(data_path, self.scope["task_id"])

    def _assert_current_authority(self, entry: Mapping[str, Any], locator: Mapping[str, Any]) -> None:
        # PlanTracker publishes packet+index in this canonical file under its
        # CAS lock/atomic replace. Read it without migration or cached state:
        # load_plan_state can write on read and is inappropriate for a tool.
        path = self.store.root.parent / "plan_state.json"
        _assert_storage_path(self.store, path)
        try:
            if path.stat().st_size > MAX_INSPECTION_AUTHORITY_BYTES:
                raise InspectionEvidenceError("INSPECTION_AUTHORITY_SIZE_LIMIT")
            with path.open("rb") as stream:
                encoded = stream.read(MAX_INSPECTION_AUTHORITY_BYTES + 1)
            if len(encoded) > MAX_INSPECTION_AUTHORITY_BYTES:
                raise InspectionEvidenceError("INSPECTION_AUTHORITY_SIZE_LIMIT")
            durable = json.loads(encoded.decode("utf-8"))
        except InspectionEvidenceError:
            raise
        except OSError as exc:
            raise InspectionEvidenceError("INSPECTION_OBJECT_IO_ERROR", f"{type(exc).__name__}: {_safe_error_detail(exc)}") from exc
        except (ValueError, TypeError) as exc:
            raise InspectionEvidenceError("INSPECTION_AUTHORITY_INVALID") from exc
        if not isinstance(durable, Mapping):
            raise InspectionEvidenceError("INSPECTION_AUTHORITY_INVALID")
        packet = assert_current_inspection_scope(durable, self.scope)
        ref = str(entry["ref_id"])
        if ref not in set(packet.get("inspection_refs") or ()) | set(packet.get("evidence_refs") or ()):
            raise InspectionEvidenceError("INSPECTION_REF_NOT_DECLARED")
        saved = ((durable.get("task_state") or {}).get("inspection_ref_registry") or {}).get(ref)
        keys = ("ref_id", "kind", "task_id", "plan_id", "step_id", "backend", "digest", "size_hint", "scope")
        if (not isinstance(saved, Mapping)
                or any(saved.get(key) != entry.get(key) for key in keys)
                or dict(saved.get("locator") or {}) != dict(locator)):
            raise InspectionEvidenceError("INSPECTION_REF_INDEX_MISMATCH")

    def read_body(self, entry: Mapping[str, Any], locator: Mapping[str, Any]) -> dict[str, Any]:
        """Read a size-bounded, immutable body after current durable authorization."""
        validate_inspection_descriptor(entry, self.scope)
        self._assert_current_authority(entry, locator)
        return self._read_verified_body(entry, locator)

    def read_causal_parent_body(self, entry, locator, *, durable, parent_ref, decision_id):
        """Publisher-only inheritance from the exact consumed diagnostic parent.

        Tools call read_body and cannot supply this grant. The binder's final
        CAS publishes these bodies under the new attempt's authorized scope.
        """
        from agent_core.workflow.recovery_projection import _failure_packet_from_record
        task = durable.get("task_state") or {}
        receipt = (task.get("typed_recovery_receipts") or {}).get(decision_id) or {}
        record = (task.get("causal_failure_packets") or {}).get(parent_ref) or {}
        parent = _failure_packet_from_record(parent_ref, record)
        consumed = (task.get("consumed_failure_packet_refs") or {}).get(parent_ref) or {}
        registered = (task.get("inspection_ref_registry") or {}).get(entry.get("ref_id"))
        expected = inspection_scope(parent, "recovery-seed-" + str(record.get("source_fingerprint") or ""))
        if (receipt.get("failure_packet_ref") != parent_ref
                or (receipt.get("directive") or {}).get("action") not in {"run_diagnostic_probe", "retry_step"}
                or consumed.get("decision_id") != decision_id
                or (task.get("causal_failure_incident_heads") or {}).get(parent.incident_id) != parent_ref
                or str(task.get("active_failure_packet_ref") or "") not in {"", parent_ref}
                or expected != self.scope or registered != entry or entry.get("locator") != locator
                or entry.get("ref_id") not in (record.get("inspection_publication") or {}).get("refs", [])
                or str(durable.get("task_id")) != parent.task_id
                or str(durable.get("plan_id")) != parent.plan_spec_ref.plan_id
                or str(durable.get("plan_version")) != parent.plan_spec_ref.plan_version
                or str(durable.get("current_step_id")) != parent.step_id):
            raise InspectionEvidenceError("DIAGNOSTIC_PARENT_SCOPE_STALE", parent_ref)
        return self._read_verified_body(entry, locator)

    def _read_verified_body(self, entry, locator):
        validate_inspection_descriptor(entry, self.scope)
        meta = dict(entry["scope"])
        if not meta.get("readable"):
            raise InspectionEvidenceError("INSPECTION_REF_NOT_RESOLVABLE", str(meta.get("unavailable_reason") or "BODY_UNAVAILABLE"))
        kind = str(meta["object_kind"])
        if set(locator) != {"object_ref", "object_type"} or locator.get("object_type") != _object_type(kind):
            raise InspectionEvidenceError("INSPECTION_BACKEND_LOCATOR_INVALID")
        object_ref = str(locator.get("object_ref") or "")
        try:
            digest = StateContractStore._parse_ref(object_ref, "object")
        except StateContractIntegrityError as exc:
            raise InspectionEvidenceError("INSPECTION_BACKEND_LOCATOR_INVALID") from exc
        if entry.get("digest") != "sha256:" + digest or entry.get("ref_id") != _handle(self.scope, kind, str(meta.get("source_object_id") or ""), "sha256:" + digest):
            raise InspectionEvidenceError("INSPECTION_OBJECT_IDENTITY_MISMATCH")
        path = self.store.object_root / (digest + ".json")
        # The generic store verifies the final file; also deny symlinked parent
        # directories here, before any content is read through the accessor.
        _assert_storage_path(self.store, path)
        try:
            if path.stat().st_size > MAX_INSPECTION_OBJECT_BYTES:
                raise InspectionEvidenceError("INSPECTION_OBJECT_SIZE_LIMIT")
            wrapper = self.store.read_object(object_ref, expected_type=_object_type(kind))
        except OSError as exc:
            raise InspectionEvidenceError("INSPECTION_OBJECT_IO_ERROR", f"{type(exc).__name__}: {_safe_error_detail(exc)}") from exc
        except StateContractIntegrityError as exc:
            cause = exc.__cause__
            if isinstance(cause, OSError):
                raise InspectionEvidenceError("INSPECTION_OBJECT_IO_ERROR", f"{type(cause).__name__}: {_safe_error_detail(cause)}") from exc
            raise InspectionEvidenceError("INSPECTION_OBJECT_INTEGRITY_ERROR", _safe_error_detail(exc)) from exc
        if (not isinstance(wrapper, Mapping) or type(wrapper.get("schema_version")) is not int
                or wrapper["schema_version"] != 1 or type(wrapper.get("object_revision")) is not int
                or wrapper["object_revision"] != 1):
            raise InspectionEvidenceError("INSPECTION_OBJECT_SCHEMA_INVALID")
        if wrapper.get("scope") != self.scope or wrapper.get("object_kind") != kind or wrapper.get("source_object_id") != meta.get("source_object_id"):
            raise InspectionEvidenceError("INSPECTION_OBJECT_IDENTITY_MISMATCH")
        body = wrapper.get("body")
        if not isinstance(body, Mapping):
            raise InspectionEvidenceError("INSPECTION_OBJECT_BODY_INVALID")
        _assert_body_scope(kind, body, self.scope)
        return deepcopy(dict(body))

    def read(self, entry: Mapping[str, Any], locator: Mapping[str, Any], *, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        body = self.read_body(entry, locator)
        meta = dict(entry["scope"])
        kind = str(meta["object_kind"])
        try:
            start = int(start_line)
            end = int(end_line) if end_line is not None else start + MAX_INSPECTION_READ_LINES - 1
            if start < 1 or end < start:
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise InspectionEvidenceError("RECOVERY_TOOL_ARGUMENT_INVALID", "inspection line range") from exc
        # Only structural JSON LF is a page boundary. splitlines() also splits
        # valid U+2028/U+0085 string contents and would corrupt reconstructed JSON.
        lines = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2).split("\n")
        representation = "json_pretty_lines_v1"
        if any(len(line) + 1 > MAX_INSPECTION_READ_CHARS for line in lines):
            # Narrowing line numbers cannot shorten one JSON string containing
            # a whole script/stdout. Offer a reversible bounded representation
            # of this same authorized body instead of an unusable retry hint.
            encoded_body = _canonical(body)
            representation = "json_chunk_envelope_v1"
            lines = json.dumps({
                "representation": representation,
                "instruction": "Concatenate content_chunks, verify body_json_sha256, then JSON-decode to recover the original scoped object. Line ranges address this representation, not source file lines.",
                "body_json_sha256": hashlib.sha256(encoded_body.encode()).hexdigest(),
                "content_chunks": [encoded_body[index:index + 2_000]
                                   for index in range(0, len(encoded_body), 2_000)],
            }, ensure_ascii=False, sort_keys=True, indent=2).split("\n")
        end = min(end, start + MAX_INSPECTION_READ_LINES - 1, len(lines))
        selected, chars, actual_end = [], 0, start - 1
        for ordinal in range(start - 1, end):
            line = lines[ordinal]
            remaining = MAX_INSPECTION_READ_CHARS - chars
            if len(line) + 1 > remaining:
                if not selected:
                    raise InspectionEvidenceError(
                        "INSPECTION_LINE_REQUIRES_NARROWING",
                        f"line={ordinal + 1}; max_chars={MAX_INSPECTION_READ_CHARS}",
                    )
                break
            selected.append(line);chars += len(line) + 1;actual_end = ordinal + 1
        truncated = actual_end < len(lines) or chars >= MAX_INSPECTION_READ_CHARS
        return {
            "tool": "evidence.read", "source_ref": entry["ref_id"],
            "source_object_id": meta["source_object_id"], "object_kind": kind,
            "object_revision": 1, "digest": entry["digest"], "scope": dict(self.scope),
            "content": "\n".join(selected), "format": "application/json",
            "representation": representation,
            "start_line": start, "end_line": actual_end, "total_lines": len(lines),
            "truncated": truncated, "cursor": {"next_start_line": actual_end + 1} if actual_end < len(lines) else {},
        }
