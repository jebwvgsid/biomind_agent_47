from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from agent_core.plan_tracker import record_provenance_event
from agent_core.workspace_attestation import (
    RUNTIME_ATTESTER_PRODUCER,
    WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
    VerifiedMutationJournalReference,
    WorkspaceAdmission,
    WorkspaceAttestationError,
    finalize_mutation_attestation,
    load_mutation_journal,
    load_verified_mutation_journal_reference,
    persist_mutation_journal,
    prepare_mutation_intents,
    start_mutation_attestation,
)


TASK_ID = "task-mutation-provenance"
PLAN_ID = "plan-mutation-provenance"
PLAN_REVISION = 3
PLAN_VERSION = "plan_v3"
STEP_ID = "step.prepare"
ACTION_ID = "action.prepare"
ACTION_DIGEST = "sha256:action-prepare"


def _canonical_digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resign_snapshot(snapshot: dict) -> dict:
    result = deepcopy(snapshot)
    result["snapshot_digest"] = _canonical_digest({
        key: value
        for key, value in result.items()
        if key != "snapshot_digest"
    })
    return result


def _resign_receipt(receipt: dict) -> dict:
    result = deepcopy(receipt)
    result.pop("immutable_hash", None)
    result.pop("receipt_id", None)
    result["receipt_id"] = (
        "workspace-receipt-"
        + _canonical_digest(result).split(":", 1)[1][:24]
    )
    result["immutable_hash"] = _canonical_digest(result)
    return result


def _resign_journal(journal: dict) -> dict:
    result = deepcopy(journal)
    result["journal_digest"] = _canonical_digest({
        key: value
        for key, value in result.items()
        if key != "journal_digest"
    })
    return result


def _state(tmp_path: Path) -> dict:
    task_root = tmp_path / "task_outputs" / TASK_ID
    task_root.mkdir(parents=True)
    plan_file = task_root / "plan.md"
    plan_file.write_text("# isolated fixture\n", encoding="utf-8")
    return {
        "task_id": TASK_ID,
        "plan_id": PLAN_ID,
        "plan_revision": PLAN_REVISION,
        "plan_version": PLAN_VERSION,
        "plan_file": str(plan_file.resolve()),
        "steps": [
            {
                "step_id": STEP_ID,
                "title": "prepare fixture workspace",
                "status": "pending",
                "inputs": [],
                "outputs": [],
                "input_artifact_ids": [],
                "output_artifact_ids": [],
            }
        ],
    }


def _bind_action_and_attempt(
    state: dict,
    *,
    action_id: str = ACTION_ID,
    step_id: str = STEP_ID,
    attempt_id: str = "attempt-1",
    candidate_fingerprint_only: bool = False,
) -> dict:
    digest_fields = (
        {"candidate_fingerprint": ACTION_DIGEST}
        if candidate_fingerprint_only
        else {"action_hash": ACTION_DIGEST}
    )
    record_provenance_event(
        state,
        "action_bound",
        {
            "action": {
                "action_id": action_id,
                "step_id": step_id,
                "plan_version": PLAN_VERSION,
                **digest_fields,
                "type": "execute",
            }
        },
    )
    return record_provenance_event(
        state,
        "attempt_created",
        {
            "attempt_id": attempt_id,
            "action_id": action_id,
            "step_id": step_id,
        },
    )


def _intent(
    tmp_path: Path,
    *,
    operation: str = "prepare_fixture_workspace",
    step_id: str = STEP_ID,
    action_id: str = ACTION_ID,
) -> dict:
    workspace = tmp_path / "formal_workspace"
    workspace.mkdir(exist_ok=True)
    admission = WorkspaceAdmission(
        status="passed",
        mutation_intents=(
            {
                "canonical_resource_id": "artifact.fixture_workspace",
                "resource_kind": "mutable_workspace",
                "formal_concrete_path": str(workspace.resolve()),
                "operation": operation,
                "mutation": "readwrite",
                "evidence_contract": {"postconditions": []},
            },
        ),
    )
    return prepare_mutation_intents(
        admission,
        task_id=TASK_ID,
        plan_id=PLAN_ID,
        plan_revision=PLAN_REVISION,
        step_id=step_id,
        action_id=action_id,
        action_digest=ACTION_DIGEST,
        action={},
    )[0]


def _journal_path(state: dict, attempt_id: str) -> Path:
    path = (
        Path(state["plan_file"]).parent
        / "attempts"
        / attempt_id
        / "mutation_intent.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _persist_started(
    state: dict,
    attempt_id: str,
    intents: list[dict],
) -> tuple[Path, dict]:
    path = _journal_path(state, attempt_id)
    persist_mutation_journal(
        path,
        start_mutation_attestation(intents, attempt_id=attempt_id),
    )
    return path, load_mutation_journal(path)


def _payload(path: Path, journal: dict) -> dict:
    return {
        "attempt_id": journal["attempt_id"],
        "journal_path": str(path.resolve()),
        "journal_digest": journal["journal_digest"],
        "intent_ids": [item["intent_id"] for item in journal["intents"]],
    }


def _register(state: dict, path: Path, journal: dict) -> dict:
    return record_provenance_event(
        state,
        WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
        _payload(path, journal),
    )


def test_t01_nonempty_event_persists_verified_attempt_reference(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    attempt = _bind_action_and_attempt(state)
    path, journal = _persist_started(
        state,
        attempt["attempt_id"],
        [_intent(tmp_path)],
    )

    result = _register(state, path, journal)
    durable = state["provenance_state"]["attempts"][attempt["attempt_id"]]
    assert durable["status"] == "CREATED"
    assert durable["workspace_mutation_intent_registration"] == result
    assert result["event_type"] == WORKSPACE_MUTATION_INTENT_RECORDED_EVENT
    assert result["producer"] == RUNTIME_ATTESTER_PRODUCER
    assert result["attempt_id"] == attempt["attempt_id"]
    assert result["journal_path"] == str(path.resolve())
    assert result["journal_digest"] == journal["journal_digest"]
    assert result["intent_ids"] == [journal["intents"][0]["intent_id"]]
    assert result["registration_digest"].startswith("sha256:")

    verified = load_verified_mutation_journal_reference(
        path,
        expected_attempt_dir=path.parent,
        expected_task_id=TASK_ID,
        expected_plan_id=PLAN_ID,
        expected_plan_revision=PLAN_REVISION,
        expected_step_id=STEP_ID,
        expected_action_id=ACTION_ID,
        expected_action_digest=ACTION_DIGEST,
        expected_attempt_id=attempt["attempt_id"],
        expected_journal_digest=journal["journal_digest"],
        expected_intent_ids=[journal["intents"][0]["intent_id"]],
    )
    assert isinstance(verified, VerifiedMutationJournalReference)
    assert verified.registration_digest == result["registration_digest"]


def test_t02_empty_not_applicable_is_a_formal_registration(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    attempt = _bind_action_and_attempt(
        state, candidate_fingerprint_only=True
    )
    path, journal = _persist_started(state, attempt["attempt_id"], [])

    result = _register(state, path, journal)
    assert journal["status"] == "not_applicable"
    assert result["journal_status"] == "not_applicable"
    assert result["intent_ids"] == []
    assert result["action_digest"] == ACTION_DIGEST
    assert attempt["action_hash"] == ACTION_DIGEST
    assert "receipts" not in result
    assert state["provenance_state"]["attempts"][attempt["attempt_id"]][
        "status"
    ] == "CREATED"


def test_t03_identical_replay_is_idempotent_and_never_resets_terminal_status(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    attempt = _bind_action_and_attempt(state)
    path, journal = _persist_started(state, attempt["attempt_id"], [])
    first = _register(state, path, journal)
    second = _register(state, path, journal)
    assert second == first

    record_provenance_event(
        state,
        "attempt_state_changed",
        {"attempt_id": attempt["attempt_id"], "status": "FAILED"},
    )
    terminal_updated_at = state["provenance_state"]["attempts"][
        attempt["attempt_id"]
    ]["updated_at"]
    replay = _register(state, path, journal)
    durable = state["provenance_state"]["attempts"][attempt["attempt_id"]]
    assert replay == first
    assert durable["status"] == "FAILED"
    assert durable["updated_at"] == terminal_updated_at
    assert durable["workspace_mutation_intent_registration"][
        "registered_at"
    ] == first["registered_at"]


def test_t03b_created_attempt_has_no_false_launch_marker_and_replay_is_scoped(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    record_provenance_event(
        state,
        "action_bound",
        {
            "action": {
                "action_id": ACTION_ID,
                "step_id": STEP_ID,
                "plan_version": PLAN_VERSION,
                "action_hash": ACTION_DIGEST,
                "generated_script_path": str(tmp_path / "generated.sh"),
                "type": "execute",
            }
        },
    )
    payload = {
        "attempt_id": "attempt-created-replay",
        "action_id": ACTION_ID,
        "step_id": STEP_ID,
    }
    created = record_provenance_event(state, "attempt_created", payload)
    assert created["status"] == "CREATED"
    assert created["attempt_sequence"] == 1
    assert created["generated_script"] == {}
    assert created["executed_script_path"] == ""
    assert record_provenance_event(
        state, "attempt_created", payload
    ) == created

    with pytest.raises(ValueError, match="Attempt replay identity conflict"):
        record_provenance_event(
            state,
            "attempt_created",
            {**payload, "step_id": "step.other"},
        )


def test_t04_same_attempt_conflicting_registration_is_rejected_without_overwrite(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    attempt = _bind_action_and_attempt(state)
    original_intent = _intent(tmp_path)
    path, journal = _persist_started(
        state, attempt["attempt_id"], [original_intent]
    )
    original = _register(state, path, journal)

    conflicting_intent = _intent(tmp_path, operation="different_operation")
    persist_mutation_journal(
        path,
        start_mutation_attestation(
            [conflicting_intent], attempt_id=attempt["attempt_id"]
        ),
    )
    conflicting = load_mutation_journal(path)
    with pytest.raises(WorkspaceAttestationError) as exc:
        _register(state, path, conflicting)
    assert exc.value.code == "MUTATION_INTENT_REGISTRATION_CONFLICT"
    durable = state["provenance_state"]["attempts"][attempt["attempt_id"]]
    assert durable["workspace_mutation_intent_registration"] == original
    assert durable["status"] == "CREATED"


def test_t05_new_attempt_gets_an_independent_registration(tmp_path: Path) -> None:
    state = _state(tmp_path)
    first_attempt = _bind_action_and_attempt(state, attempt_id="attempt-1")
    base_intent = _intent(tmp_path)
    first_path, first_journal = _persist_started(
        state, "attempt-1", [base_intent]
    )
    first = _register(state, first_path, first_journal)
    record_provenance_event(
        state,
        "attempt_state_changed",
        {"attempt_id": first_attempt["attempt_id"], "status": "FAILED"},
    )

    second_attempt = _bind_action_and_attempt(state, attempt_id="attempt-2")
    second_intent = deepcopy(base_intent)
    second_path, second_journal = _persist_started(
        state, "attempt-2", [second_intent]
    )
    second = _register(state, second_path, second_journal)

    attempts = state["provenance_state"]["attempts"]
    assert attempts["attempt-1"]["workspace_mutation_intent_registration"] == first
    assert attempts["attempt-2"]["workspace_mutation_intent_registration"] == second
    assert first["attempt_id"] != second["attempt_id"]
    assert first["registration_digest"] != second["registration_digest"]


def test_t06_unknown_attempt_and_cross_scope_journal_are_rejected(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    attempt = _bind_action_and_attempt(state)
    path, journal = _persist_started(state, "unknown-attempt", [])
    with pytest.raises(WorkspaceAttestationError) as unknown:
        _register(state, path, journal)
    assert unknown.value.code == "MUTATION_INTENT_ATTEMPT_UNKNOWN"
    assert "unknown-attempt" not in state["provenance_state"]["attempts"]

    cross_scope = _intent(
        tmp_path,
        step_id="step.other",
    )
    path, journal = _persist_started(
        state, attempt["attempt_id"], [cross_scope]
    )
    with pytest.raises(WorkspaceAttestationError) as mismatch:
        _register(state, path, journal)
    assert mismatch.value.code == "MUTATION_JOURNAL_SCOPE_MISMATCH"
    assert "workspace_mutation_intent_registration" not in state[
        "provenance_state"
    ]["attempts"][attempt["attempt_id"]]


def test_t07_payload_path_source_digest_and_ids_fail_closed(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    attempt = _bind_action_and_attempt(state)
    intent = _intent(tmp_path)
    path, journal = _persist_started(state, attempt["attempt_id"], [intent])

    invalid_payloads = []
    bad_type = _payload(path, journal)
    bad_type["intent_ids"] = tuple(bad_type["intent_ids"])
    invalid_payloads.append((bad_type, "MUTATION_INTENT_EVENT_PAYLOAD_INVALID"))
    duplicate_ids = _payload(path, journal)
    duplicate_ids["intent_ids"] = [
        "mutation-intent-fixture",
        "mutation-intent-fixture",
    ]
    invalid_payloads.append(
        (duplicate_ids, "MUTATION_JOURNAL_INTENT_IDS_INVALID")
    )
    wrong_digest = _payload(path, journal)
    wrong_digest["journal_digest"] = "sha256:" + "0" * 64
    invalid_payloads.append(
        (wrong_digest, "MUTATION_JOURNAL_DIGEST_MISMATCH")
    )
    for payload, code in invalid_payloads:
        with pytest.raises(WorkspaceAttestationError) as exc:
            record_provenance_event(
                state, WORKSPACE_MUTATION_INTENT_RECORDED_EVENT, payload
            )
        assert exc.value.code == code

    wrong_root = tmp_path / "untrusted" / attempt["attempt_id"]
    wrong_root.mkdir(parents=True)
    wrong_path = wrong_root / "mutation_intent.json"
    persist_mutation_journal(wrong_path, journal)
    with pytest.raises(WorkspaceAttestationError) as path_error:
        record_provenance_event(
            state,
            WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
            _payload(wrong_path, journal),
        )
    assert path_error.value.code == "MUTATION_JOURNAL_PATH_SCOPE_MISMATCH"

    tampered_intent = deepcopy(journal)
    tampered_intent["intents"][0]["formal_concrete_path"] = str(
        (tmp_path / "different_workspace").resolve()
    )
    tampered_intent["journal_digest"] = _canonical_digest(
        {
            key: value
            for key, value in tampered_intent.items()
            if key != "journal_digest"
        }
    )
    persist_mutation_journal(path, tampered_intent)
    with pytest.raises(WorkspaceAttestationError) as intent_integrity_error:
        record_provenance_event(
            state,
            WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
            _payload(path, tampered_intent),
        )
    assert (
        intent_integrity_error.value.code
        == "MUTATION_JOURNAL_INTENT_ID_INTEGRITY_INVALID"
    )

    forged = deepcopy(journal)
    forged["producer"] = "untrusted.fixture"
    forged["journal_digest"] = _canonical_digest(
        {key: value for key, value in forged.items() if key != "journal_digest"}
    )
    persist_mutation_journal(path, forged)
    with pytest.raises(WorkspaceAttestationError) as source_error:
        record_provenance_event(
            state,
            WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
            _payload(path, forged),
        )
    assert source_error.value.code == "MUTATION_JOURNAL_INTEGRITY_INVALID"
    assert "workspace_mutation_intent_registration" not in state[
        "provenance_state"
    ]["attempts"][attempt["attempt_id"]]


def test_t07b_before_snapshot_shape_digest_root_and_reliability_fail_closed(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    attempt = _bind_action_and_attempt(state)
    path, started = _persist_started(
        state,
        attempt["attempt_id"],
        [_intent(tmp_path)],
    )

    invalid_type = deepcopy(started)
    invalid_type["intents"][0]["before_evidence"] = []

    wrong_root = deepcopy(started)
    wrong_root_snapshot = deepcopy(
        wrong_root["intents"][0]["before_evidence"]
    )
    wrong_root_snapshot["root"] = str(
        (tmp_path / "different_workspace").resolve()
    )
    wrong_root["intents"][0]["before_evidence"] = _resign_snapshot(
        wrong_root_snapshot
    )

    invalid_internal_digest = deepcopy(started)
    invalid_internal_digest["intents"][0]["before_evidence"][
        "root_state"
    ]["kind"] = "forged_kind"

    unreliable = deepcopy(started)
    unreliable_snapshot = deepcopy(
        unreliable["intents"][0]["before_evidence"]
    )
    unreliable_snapshot["reliable"] = False
    unreliable_snapshot["unknown_reason"] = "fixture_unreliable"
    unreliable["intents"][0]["before_evidence"] = _resign_snapshot(
        unreliable_snapshot
    )

    cases = (
        (invalid_type, "MUTATION_JOURNAL_SNAPSHOT_INVALID"),
        (wrong_root, "MUTATION_JOURNAL_SNAPSHOT_INVALID"),
        (invalid_internal_digest, "MUTATION_JOURNAL_SNAPSHOT_INVALID"),
        (unreliable, "MUTATION_JOURNAL_SNAPSHOT_UNRELIABLE"),
    )
    for forged, expected_code in cases:
        forged = _resign_journal(forged)
        persist_mutation_journal(path, forged)
        with pytest.raises(WorkspaceAttestationError) as exc:
            _register(state, path, forged)
        assert exc.value.code == expected_code

    assert "workspace_mutation_intent_registration" not in state[
        "provenance_state"
    ]["attempts"][attempt["attempt_id"]]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("receipt", "MUTATION_JOURNAL_LIFECYCLE_INVALID"),
        ("after_evidence", "MUTATION_JOURNAL_LIFECYCLE_INVALID"),
        ("postcondition_results", "MUTATION_JOURNAL_LIFECYCLE_INVALID"),
        ("receipt_id", "MUTATION_JOURNAL_LIFECYCLE_INVALID"),
        ("intent_status", "MUTATION_JOURNAL_LIFECYCLE_INVALID"),
        ("finished_at", "MUTATION_JOURNAL_LIFECYCLE_INVALID"),
    ],
)
def test_t07c_attempt_started_rejects_post_execution_state(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    state = _state(tmp_path)
    attempt = _bind_action_and_attempt(state)
    path, started = _persist_started(
        state,
        attempt["attempt_id"],
        [_intent(tmp_path)],
    )
    forged = deepcopy(started)
    if mutation == "receipt":
        forged["receipts"] = [{"receipt_id": "forged"}]
    elif mutation == "after_evidence":
        forged["intents"][0]["after_evidence"] = deepcopy(
            forged["intents"][0]["before_evidence"]
        )
    elif mutation == "postcondition_results":
        forged["intents"][0]["postcondition_results"] = []
    elif mutation == "receipt_id":
        forged["intents"][0]["receipt_id"] = "workspace-receipt-forged"
    elif mutation == "finished_at":
        forged["finished_at"] = ""
    else:
        forged["intents"][0]["status"] = "unknown"
    forged = _resign_journal(forged)
    persist_mutation_journal(path, forged)

    with pytest.raises(WorkspaceAttestationError) as exc:
        _register(state, path, forged)
    assert exc.value.code == expected_code
    assert "workspace_mutation_intent_registration" not in state[
        "provenance_state"
    ]["attempts"][attempt["attempt_id"]]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("cardinality", "MUTATION_JOURNAL_RECEIPT_CARDINALITY_INVALID"),
        ("scope", "MUTATION_JOURNAL_RECEIPT_SCOPE_MISMATCH"),
        ("receipt_hash", "MUTATION_JOURNAL_RECEIPT_INTEGRITY_INVALID"),
        ("after_shape", "MUTATION_JOURNAL_SNAPSHOT_INVALID"),
        ("after_digest", "MUTATION_JOURNAL_SNAPSHOT_INVALID"),
        ("attested_status", "MUTATION_JOURNAL_LIFECYCLE_INVALID"),
    ],
)
def test_t07d_finalized_receipt_and_after_snapshot_fail_closed(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    state = _state(tmp_path)
    attempt = _bind_action_and_attempt(state)
    path, started = _persist_started(
        state,
        attempt["attempt_id"],
        [_intent(tmp_path)],
    )
    forged = finalize_mutation_attestation(started, process_exit_code=None)
    if mutation == "cardinality":
        forged["receipts"] = []
    elif mutation == "scope":
        receipt = deepcopy(forged["receipts"][0])
        receipt["formal_concrete_path"] = str(
            (tmp_path / "forged_workspace").resolve()
        )
        forged["receipts"][0] = _resign_receipt(receipt)
    elif mutation == "receipt_hash":
        forged["receipts"][0]["unknown_reason"] = "forged_reason"
    elif mutation == "after_shape":
        forged["intents"][0]["after_evidence"] = []
        receipt = deepcopy(forged["receipts"][0])
        receipt["after_evidence"] = []
        receipt = _resign_receipt(receipt)
        forged["receipts"][0] = receipt
        forged["intents"][0]["receipt_id"] = receipt["receipt_id"]
    elif mutation == "after_digest":
        forged["intents"][0]["after_evidence"]["root_state"][
            "kind"
        ] = "forged_kind"
        receipt = deepcopy(forged["receipts"][0])
        receipt["after_evidence"] = deepcopy(
            forged["intents"][0]["after_evidence"]
        )
        receipt = _resign_receipt(receipt)
        forged["receipts"][0] = receipt
        forged["intents"][0]["receipt_id"] = receipt["receipt_id"]
    else:
        forged["receipts"][0]["attested"] = True
        receipt = _resign_receipt(forged["receipts"][0])
        forged["receipts"][0] = receipt
        forged["intents"][0]["receipt_id"] = receipt["receipt_id"]
        forged["status"] = "completed"
    forged = _resign_journal(forged)
    persist_mutation_journal(path, forged)

    with pytest.raises(WorkspaceAttestationError) as exc:
        _register(state, path, forged)
    assert exc.value.code == expected_code
    assert "workspace_mutation_intent_registration" not in state[
        "provenance_state"
    ]["attempts"][attempt["attempt_id"]]


@pytest.mark.parametrize("empty", [False, True])
def test_t08_registration_digest_survives_finalize_and_replay(
    tmp_path: Path,
    empty: bool,
) -> None:
    state = _state(tmp_path)
    attempt = _bind_action_and_attempt(state)
    intents = [] if empty else [_intent(tmp_path)]
    path, started = _persist_started(state, attempt["attempt_id"], intents)
    initial_payload = _payload(path, started)
    registered = _register(state, path, started)

    finalized = finalize_mutation_attestation(started, process_exit_code=None)
    persist_mutation_journal(path, finalized)
    finalized = load_mutation_journal(path)
    exact_initial_replay = record_provenance_event(
        state,
        WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
        initial_payload,
    )
    replay = _register(state, path, finalized)

    assert finalized["journal_digest"] != started["journal_digest"]
    assert exact_initial_replay == registered
    assert replay == registered
    verified_final = load_verified_mutation_journal_reference(
        path,
        expected_attempt_dir=path.parent,
        expected_task_id=TASK_ID,
        expected_plan_id=PLAN_ID,
        expected_plan_revision=PLAN_REVISION,
        expected_step_id=STEP_ID,
        expected_action_id=ACTION_ID,
        expected_action_digest=ACTION_DIGEST,
        expected_attempt_id=attempt["attempt_id"],
        expected_journal_digest=finalized["journal_digest"],
        expected_intent_ids=[item["intent_id"] for item in intents],
    )
    assert verified_final.registration_digest == registered[
        "registration_digest"
    ]


def test_t08b_exact_replay_rejects_corrupt_durable_registration(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    attempt = _bind_action_and_attempt(state)
    path, started = _persist_started(
        state,
        attempt["attempt_id"],
        [_intent(tmp_path)],
    )
    initial_payload = _payload(path, started)
    _register(state, path, started)
    durable = state["provenance_state"]["attempts"][attempt["attempt_id"]]
    durable["workspace_mutation_intent_registration"]["producer"] = (
        "untrusted.fixture"
    )

    with pytest.raises(WorkspaceAttestationError) as exc:
        record_provenance_event(
            state,
            WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
            initial_payload,
        )
    assert exc.value.code == "MUTATION_INTENT_DURABLE_REGISTRATION_INVALID"


def test_t17_unknown_event_remains_an_explicit_protocol_error(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    with pytest.raises(ValueError, match="Unsupported provenance event"):
        record_provenance_event(state, "unknown_fixture_event", {})


def test_t23_existing_provenance_events_and_schema_one_records_still_work(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    attempt = _bind_action_and_attempt(state)
    for status in ("SCRIPT_READY", "APPROVED", "STARTING", "RUNNING"):
        record_provenance_event(
            state,
            "attempt_state_changed",
            {"attempt_id": attempt["attempt_id"], "status": status},
        )
    result = record_provenance_event(
        state,
        "execution_finished",
        {
            "attempt_id": attempt["attempt_id"],
            "execution_record": {
                "execution_id": "execution-fixture",
                "exit_code": 1,
                "produced_artifacts": [],
            },
        },
    )
    assert state["provenance_state"]["schema_version"] == 1
    assert result["status"] == "EVIDENCE_COMMITTED"
    assert result["returncode"] == 1
    assert "workspace_mutation_intent_registration" not in result
