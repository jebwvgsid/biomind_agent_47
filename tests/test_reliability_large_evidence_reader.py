"""Long script/stdout objects remain readable through real scoped transport.

Execution facts are recorded fixtures, never executed. Approval, provenance,
causal publication, immutable store, registry, reader and Recovery ToolMessages
use production code. No observation or Recovery budget is increased.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from agent_core.plan_tracker import load_plan_state, record_provenance_event, save_plan_state
from agent_core.state_contract_store import StateContractStore
from agent_core.step_effects import structured_effect_parse_receipt
from agent_core.workflow.inspection_evidence import (
    INSPECTION_SCOPE_FIELDS, InspectionEvidenceError, InspectionObjectReader,
)
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder
from agent_core.workflow.scoped_context import provider_visible_payload
from tests.test_inspection_workflow import _tag_json
from tests.test_reliability_evidence_publication import _artifact_case, _object_rows, _reopen, _source


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _large_failure(root, kind):
    state, _paths, _text = _artifact_case(root, create_attempt=False)
    plan = load_plan_state(root, state["task_id"])
    action = deepcopy(state["pending_action"])
    action["action_id"] = "recorded-large-" + kind
    long_text = 'START-中文-"\\-\u2028-\u0085-' + "x" * 20_050 + "-END"
    action["code"] = "# recorded-only script\n# " + long_text + "\nexit 17\n" if kind == "script" else "exit 17"
    record_provenance_event(plan, "action_bound", {"action": action, "raw_action": action})
    attempt = record_provenance_event(plan, "attempt_started", {
        "action_id": action["action_id"], "step_id": action["step_id"]})
    stdout = long_text if kind == "execution" else "RECORDED_FAILURE\n"
    execution = {"schema_version": 1, "task_id": state["task_id"],
        **{key: action[key] for key in ("plan_id", "plan_version", "step_id", "action_id", "code")},
        "attempt_id": attempt["attempt_id"], "execution_id": "recorded-large-" + kind,
        "returncode": 17, "execution_status": "failed", "raw_stdout": stdout,
        "raw_stderr": "RECORDED_ERROR", "effect_evidence": {"parser_receipt":
            structured_effect_parse_receipt(stdout, action["expected_step_effect"])}}
    record_provenance_event(plan, "execution_finished", {
        "attempt_id": attempt["attempt_id"], "execution_record": execution})
    save_plan_state(root, state["task_id"], plan)
    state.update(pending_action=action, execution_evidence=execution)
    source = _source(state, "execute_outcome", "execution_failed")
    state.update(CausalFailurePacketBinder(data_path=str(root)).bind(state, "execute_outcome", source))
    durable, seed, registry, dispatcher = _reopen(root, state["task_id"])
    row = _object_rows(registry)[kind]
    assert row["readable"]
    entry, locator = registry.resolve(row["ref_id"], kinds=("evidence",))
    wrapper = StateContractStore(root, state["task_id"]).read_object(
        locator["object_ref"], expected_type=locator["object_type"])
    expected = wrapper["body"]
    expected_field = expected["scripts"]["code"] if kind == "script" else expected["execution"]["raw_stdout"]
    assert expected_field == (action["code"] if kind == "script" else stdout)
    assert max(map(len, json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=2).splitlines())) > 16_000
    assert row["digest"] == locator["object_ref"].removeprefix("object:")
    return state, seed, registry, dispatcher, row, entry, locator, expected, expected_field


class _ReadLargeObject:
    def __init__(self, row, expected):
        self.row, self.expected = row, expected
        self.requests, self.pages, self.pointers = [], [], []
        self.seen, self.body = set(), None

    async def ainvoke(self, messages, **_kwargs):
        self.requests.append([provider_visible_payload(message) for message in messages])
        seed = _tag_json(list(messages), "recovery_seed")
        assert any(row["ref_id"] == self.row["ref_id"] for row in seed["inspection_manifest"])
        for index, message in enumerate(messages):
            if message.type != "tool":
                continue
            envelope = json.loads(message.content)
            request_id = envelope.get("request_id", "")
            if request_id in self.seen or not request_id.startswith("large-read-"):
                continue
            self.seen.add(request_id)
            assert not envelope["error"], envelope
            page = envelope["result"]
            assert page.get("status") != "EXTERNALIZED", page
            assert page["representation"] == "json_chunk_envelope_v1"
            assert page["source_ref"] == self.row["ref_id"] and page["digest"] == self.row["digest"]
            assert page["start_line"] == (self.pages[-1]["end_line"] + 1 if self.pages else 1)
            self.pages.append(page)
            self.pointers.append({"request": len(self.requests) - 1, "message": index,
                "start_line": page["start_line"], "end_line": page["end_line"],
                "page_sha256": hashlib.sha256(page["content"].encode()).hexdigest()})
        if self.pages and not self.pages[-1]["truncated"]:
            representation = json.loads("\n".join(page["content"] for page in self.pages))
            assert representation["representation"] == "json_chunk_envelope_v1"
            chunks = representation["content_chunks"]
            assert chunks and all(isinstance(chunk, str) and len(chunk) <= 2_000 for chunk in chunks)
            canonical = "".join(chunks)
            assert hashlib.sha256(canonical.encode()).hexdigest() == representation["body_json_sha256"]
            self.body = json.loads(canonical)
            assert self.body == self.expected
            return AIMessage(content=json.dumps({"schema_version": 1,
                "directive_id": "large-evidence-inspected", "task_id": seed["invocation_scope"]["task_id"],
                "action": "blocked", "summary": "The recorded long evidence was inspected; a separately approved correction is still required.",
                "reason_code": "OFFLINE_LARGE_EVIDENCE_INSPECTED"}))
        start = self.pages[-1]["cursor"]["next_start_line"] if self.pages else 1
        return AIMessage(content="", tool_calls=[{"id": f"large-read-{start}",
            "name": "evidence_read", "type": "tool_call",
            "args": {"evidence_ref": self.row["ref_id"], "start_line": start}}])




def test_long_field_representation_does_not_relax_scope_checks(tmp_path):
    _state, _seed, _registry, _dispatcher, row, entry, locator, _expected, _field = _large_failure(tmp_path, "script")
    for field in ("step_id", "plan_version"):
        scope = {key: row["scope"].get(key, "") for key in INSPECTION_SCOPE_FIELDS}
        scope[field] = "different-scope-" + field
        reader = InspectionObjectReader(str(tmp_path), scope=scope)
        with patch.object(reader.store, "read_object", side_effect=AssertionError("cross-scope body read")) as read:
            with pytest.raises(InspectionEvidenceError, match="INSPECTION_REF_SCOPE_MISMATCH"):
                reader.read(entry.to_dict(), locator)
            assert read.call_count == 0
