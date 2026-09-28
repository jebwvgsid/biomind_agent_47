"""Real diagnostic detour and corrective action; synthetic fixtures, no science.

The only subprocess eligible to run is the tiny, pinned engineering reader below.
A guarded candidate bootstrap must explicitly allow that exact script launch.
All Plan/Skill approval, Recovery admission, artifact reads, graph routing,
verification and CommitStep remain production implementations.
"""
from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
import re
import shlex
import shutil
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from agent_core.plan_tracker import load_plan_state
from agent_core.skill_access import SkillAccessService
from agent_core.skill_manager import SkillManager
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder, bind_causal_failure_node
from agent_core.workflow.scoped_context import provider_visible_payload
from tests import test_generate_session_1788681718_production_path as gen_fixture
from tests.test_inspection_workflow import InspectionReviewer, _tag_json

BINARY = b"\x00BIOMIND-ENGINEERING-PROBE\x01"
BLOB_ID = "artifact-diagnostic-engineering-blob"
OPERATION_ID = "bounded_engineering_binary_header"
PROGRAM = '''import json, os, stat, sys
if len(sys.argv) != 2:
    raise SystemExit(64)
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
with os.fdopen(fd, "rb") as stream:
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        raise SystemExit(65)
    payload = stream.read(65)
if len(payload) > 64:
    raise SystemExit(66)
print(json.dumps({"probe_valid": payload.startswith(bytes([0, 66, 73, 79])), "inspected_bytes": len(payload), "coverage": "synthetic engineering object only"}, sort_keys=True))
'''


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def _json_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_objects(child)
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return
        if decoded != value:
            yield from _json_objects(decoded)


def _exact_inherited_evidence(messages, object_kind):
    found = []
    for index, message in enumerate(messages):
        content = str(message.content)
        match = re.fullmatch(
            r'<inherited_recovery_evidence schema_version="1">\n(.*?)\n'
            r'---BEGIN EXACT SOURCE BODY---\n(.*?)\n'
            r'---END EXACT SOURCE BODY---\n</inherited_recovery_evidence>', content, re.S)
        if not match:
            continue
        metadata, body_text = json.loads(match.group(1)), match.group(2)
        if metadata.get("object_kind") != object_kind:
            continue
        digest = hashlib.sha256(body_text.encode()).hexdigest()
        assert metadata["content_hash"].removeprefix("sha256:") == digest
        found.append({"message_index": index, "metadata": metadata,
                      "body_sha256": digest, "body": json.loads(body_text)})
    assert len(found) == 1, (object_kind, found)
    return found[0]


def _prepare_skill_before_selection(fixture):
    import yaml
    source = Path(fixture.manager.get_skill_dir(gen_fixture.SKILL_ID))
    destination = fixture.root / "selected-skills" / "genomics" / "snpeff-annotation"
    shutil.copytree(source, destination)
    program = destination / "scripts" / "engineering_binary_probe.py"
    program.parent.mkdir(parents=True, exist_ok=True)
    program.write_text(PROGRAM)
    skill = destination / "SKILL.md"
    original = skill.read_text()
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\n|$)", original, re.S)
    assert match
    metadata = yaml.safe_load(match.group(1))
    contract = metadata.setdefault("metadata", {}).setdefault("generate", {})
    argv = [sys.executable, "-I", str(program), "{input:0}"]
    contract["diagnostic_operations"] = [{
        "operation_id": OPERATION_ID, "read_only": True, "argv": argv,
        "limits": {"max_wall_seconds": 5, "max_input_bytes": 64,
                   "max_output_bytes": 512, "max_threads": 1, "max_files": 1},
        "description": "Read one small synthetic binary object's header with a bounded engineering reader; do not establish scientific validity.",
    }]
    skill.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True) + "---\n" + original[match.end():])
    fixture.manager = SkillManager(str(fixture.root / "selected-skills"))
    fixture.service = SkillAccessService(fixture.manager)
    fixture.agent.skill_manager = fixture.manager
    fixture.agent.skill_access_service = fixture.service
    blob = fixture.root / "input-data" / "engineering-object.bin"
    blob.write_bytes(BINARY)
    fixture.path_by_handle["input.diagnostic_blob"] = blob
    fixture.plan_fixture["task_inputs"].append({
        "artifact_id": BLOB_ID, "semantic_handle": "input.diagnostic_blob",
        "role": "diagnostic_blob", "required": False, "format": "binary",
    })
    fixture.plan_fixture["candidate"]["steps"][0]["inputs"].append(BLOB_ID)
    return program, blob, [sys.executable, "-I", str(program), str(blob)]


class _DiagnosticRecoveryModel:
    def __init__(self, fixture, task_id, evidence_dir):
        self.fixture, self.task_id, self.evidence_dir = fixture, task_id, evidence_dir
        self.requests = []
        self.phase = "read_binary"
        self.incident_id = None
        self.post_probe_durable = None
        self.probe_result = None
        self.original_execution = None
        self.original_scripts = None
        self.binary_tool_error = None

    async def ainvoke(self, messages, **_kwargs):
        wire = [provider_visible_payload(message) for message in messages]
        self.requests.append(wire)
        _save(self.evidence_dir / ("recovery-request-%02d.json" % len(self.requests)), wire)
        seed = _tag_json(list(messages), "recovery_seed")
        packet = seed["failure_packet"]
        current_incident = packet["incident_id"]
        if self.incident_id is None:
            self.incident_id = current_incident
        assert current_incident == self.incident_id, "diagnostic return must preserve the original incident"
        manifest = seed["inspection_manifest"]
        tools = [json.loads(message.content) for message in messages if message.type == "tool"]
        common = {"schema_version": 1, "task_id": self.task_id,
                  "target_step_id": seed["invocation_scope"]["step_id"],
                  "evidence_refs": [packet["source_outcome_ref"]]}
        if self.phase == "read_binary":
            self.original_execution = _exact_inherited_evidence(messages, "execution")
            self.original_scripts = _exact_inherited_evidence(messages, "script")
            entry = next(row for row in manifest if row["ref_id"] == "artifact:" + BLOB_ID)
            self.phase = "request_probe"
            return AIMessage(content="", tool_calls=[{"id": "read-engineering-binary", "name": "artifact_read", "type": "tool_call",
                "args": {"ref_id": entry["ref_id"], "representation": "text", "start_line": 1, "end_line": 2}}])
        if self.phase == "request_probe":
            result = next(row for row in tools if row["request_id"] == "read-engineering-binary")
            assert result["error"]["code"] == "READ_REPRESENTATION_UNSUPPORTED", result
            assert "PERMISSION" not in result["error"]["code"]
            self.binary_tool_error = deepcopy(result)
            self.phase = "read_probe_result"
            return AIMessage(content=json.dumps({**common, "directive_id": "inspect-binary-header",
                "action": "run_diagnostic_probe", "reason_code": "AUTHORIZED_OBJECT_REPRESENTATION_UNSUPPORTED",
                "summary": "The scoped binary object needs its declared engineering reader.",
                "instruction": "Inspect the declared binary object using the selected bounded diagnostic operation, then return to this same investigation.",
                "diagnostic_operation": gen_fixture.SKILL_ID + "#" + OPERATION_ID,
                "diagnostic_inputs": ["artifact:" + BLOB_ID]}))
        if self.phase == "read_probe_result":
            inherited = _exact_inherited_evidence(messages, "causal_history")
            history = inherited["body"]
            assert history["original_failure"]["failure_type"] == "execution_failed"
            assert history["original_failure"]["error_code"] == "EXECUTION_FAILED"
            assert history["original_failure"]["incident_id"] == self.incident_id
            execution_index, parent_execution = next((index, row) for index, row in enumerate(history["parent_evidence"]) if row["object_kind"] == "execution")
            script_index, parent_scripts = next((index, row) for index, row in enumerate(history["parent_evidence"]) if row["object_kind"] == "script")
            assert parent_execution["body"] == self.original_execution["body"]
            assert parent_scripts["body"] == self.original_scripts["body"]
            assert parent_execution["body"]["execution"]["returncode"] == 1
            assert "print $3" in parent_scripts["body"]["scripts"]["code"]
            error_index, prior_error = next((index, row) for index, row in enumerate(history["prior_tool_results"]) if row["request_id"] == "read-engineering-binary")
            assert prior_error == self.binary_tool_error
            _save(self.evidence_dir / "causal-history-provider-receipt.json", {
                "request_file": "recovery-request-%02d.json" % len(self.requests),
                "message_pointer": "/%d/content" % inherited["message_index"],
                "metadata": inherited["metadata"], "body_sha256": inherited["body_sha256"],
                "original_execution_source_digest": parent_execution["source_digest"],
                "original_script_source_digest": parent_scripts["source_digest"],
                "original_execution_sha256": self.original_execution["body_sha256"],
                "original_script_sha256": self.original_scripts["body_sha256"],
                "asserted_body_pointers": ["/original_failure", f"/parent_evidence/{execution_index}/body", f"/parent_evidence/{script_index}/body", f"/prior_tool_results/{error_index}"],
                "original_returncode": 1, "prior_tool_error": prior_error["error"]["code"],
            })
            durable = load_plan_state(self.fixture.root, self.task_id)
            self.post_probe_durable = deepcopy(durable)
            assert not durable["task_state"].get("typed_step_commit_receipts")
            assert durable["current_step_id"] == "step.check_contigs"
            assert durable["task_state"]["typed_recovery_budget"][self.incident_id] == 1
            candidates = [row for row in manifest if row.get("kind") == "diagnostic"]
            if not candidates:
                candidates = [row for row in manifest if row.get("kind") == "execution" or row.get("scope", {}).get("object_kind") == "execution"]
            assert len(candidates) == 1, candidates
            self.phase = "repair_primary"
            return AIMessage(content="", tool_calls=[{"id": "read-diagnostic-result", "name": "evidence_read", "type": "tool_call",
                "args": {"evidence_ref": candidates[0]["ref_id"]}}])
        assert self.phase == "repair_primary", self.phase
        result = next(row for row in tools if row["request_id"] == "read-diagnostic-result")
        assert not result["error"], result
        observed = [row for row in _json_objects(result) if row.get("probe_valid") is True and row.get("inspected_bytes") == len(BINARY)]
        assert observed, "actual diagnostic output must reach ToolMessage and this next provider request"
        self.probe_result = observed[0]
        self.phase = "done"
        return AIMessage(content=json.dumps({**common, "directive_id": "repair-primary-after-diagnostic",
            "action": "retry_step", "reason_code": "GENERATED_ACTION_FAILED",
            "summary": "The diagnostic supplied the missing representation evidence; the primary extractor still needs correction.",
            "instruction": "Correct the VCF header extractor under the unchanged approved comparison method; produce a new action, request fresh approval and emit the required contigs_compatible observation."}))


