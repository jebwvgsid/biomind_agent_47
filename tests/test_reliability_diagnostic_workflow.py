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
import bio_agents
from agent_core.plan_tracker import load_plan_state
from agent_core.skill_access import SkillAccessService
from agent_core.skill_manager import SkillManager
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder, bind_causal_failure_node
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from agent_core.workflow.scoped_context import provider_visible_payload
from agent_core.workflow.verifier import TypedSupervisor
from supervisor import SupervisorNode
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


def test_scoped_diagnostic_returns_to_recovery_then_corrects_primary_and_commits(tmp_path):
    async def scenario():
        scenario_caller = asyncio.current_task()
        next_request_seen = False
        fixture = gen_fixture.Session1788681718GenerateProductionPathTests(methodName="test_adm_01_saved_candidate_reaches_fake_executor_once")
        await fixture.asyncSetUp()
        evidence = tmp_path / "evidence"
        events, dispatches, approvals = [], [], []
        task_id = "reliability-diagnostic-new-primary"
        model = None
        try:
            program, blob, approved_argv = _prepare_skill_before_selection(fixture)
            state = fixture._approved_generate_state(task_id)
            approved_initial = load_plan_state(fixture.root, task_id)
            _save(evidence / "initial-approved-state.json", approved_initial)
            vcf = fixture.path_by_handle["input.variant_vcf"]
            vcf.write_text(vcf.read_text().replace("#CHROM", "##contig=<ID=chr1,length=1>\n#CHROM"))
            first = json.loads(fixture.saved_response_content.strip().removeprefix("```json").removesuffix("```"))
            old = "vcf_contigs=$(grep -v '^#' \"$vcf\" | cut -f1 | sort -u)"
            assert old in first["argv"][2]
            first["argv"][2] = first["argv"][2].replace(old, "vcf_contigs=$(awk -F'[=<,]' '/^##contig=<ID=/{print $3}' \"$vcf\" | sort -u)")
            repaired = deepcopy(first)
            repaired["argv"][2] = repaired["argv"][2].replace("print $3", "print $4")
            probe_code = shlex.join(approved_argv)
            probe = {"type": "execute", "language": "bash", "purpose": "probe",
                     "operation": "check_contig_compatibility", "argv": approved_argv,
                     "code": probe_code, "env": {}, "outputs": []}
            fixture.provider.responses = [json.dumps(first), json.dumps(probe), json.dumps(repaired)]
            fixture.provider.calls.clear()
            fixture.agent.middleware.auto_approve_safe = False
            model = _DiagnosticRecoveryModel(fixture, task_id, evidence)
            recovery = Schema2RecoveryNode(data_path=str(fixture.root), model=model,
                phase_store=fixture.agent.phase_archive_store, skill_service=fixture.service)
            reviewer = InspectionReviewer(accept_first=True)
            supervisor = bind_causal_failure_node(TypedSupervisor(data_path=str(fixture.root),
                verifier=SupervisorNode(reviewer, output_dir=str(fixture.root / "task_outputs")).run).run,
                binder=CausalFailurePacketBinder(data_path=str(fixture.root)), outcome_field="verification_outcome")
            handlers = fixture.agent._schema2_runtime_node_handlers
            async def observed(name, handler, node_state):
                result = await handler(node_state)
                events.append({"node": name, "result": deepcopy(result)})
                _save(evidence / "graph-events.json", events)
                return result
            configured = {**{key: handlers[key] for key in ("generate", "execute", "commit_step", "final_audit")},
                          "supervisor": supervisor, "recovery": recovery.run}
            def observe_handler(name, handler):
                async def invoke(node_state):
                    return await observed(name, handler, node_state)
                return invoke
            wrapped = {name: observe_handler(name, handler) for name, handler in configured.items()}
            graph = build_schema2_runtime_graph(wrapped).compile(checkpointer=InMemorySaver())
            config = {"configurable": {"thread_id": task_id}, "recursion_limit": 40}
            real_runner = bio_agents.run_script_path
            provider_call = fixture.provider.ainvoke
            async def provider(messages, **kwargs):
                nonlocal next_request_seen
                if len(fixture.provider.calls) == 3:
                    fixture.provider.calls.append([provider_visible_payload(item) for item in messages])
                    current = load_plan_state(fixture.root, task_id)
                    assert current["current_step_id"] == "step.prepare_database"
                    next_request_seen = True
                    # Cancel the real caller after capturing its request. A
                    # provider that merely raises CancelledError represents a
                    # different client-failure policy and may be retried.
                    scenario_caller.cancel("isolated test reached next step provider")
                    await asyncio.Future()
                return await provider_call(messages, **kwargs)
            async def selective_runner(path, *, lifecycle_callback=None, **kwargs):
                durable = load_plan_state(fixture.root, task_id)
                pending = durable["task_state"]["pending_action"]
                envelope = pending.get("recovery_execution_envelope") or {}
                diagnostic = envelope.get("result_contract") == "DIAGNOSTIC_EVIDENCE"
                row = {"path": str(path), "diagnostic": diagnostic, "action_id": pending["action_id"]}
                dispatches.append(row)
                if diagnostic:
                    assert program.read_text() == PROGRAM
                    assert blob.read_bytes() == BINARY
                    assert len([item for item in dispatches if item["diagnostic"]]) == 1
                    assert not durable["task_state"].get("typed_step_commit_receipts")
                    row["guard_contract"] = {"script_path": str(path), "script_sha256": kwargs["expected_sha256"],
                        "program_path": str(program), "program_sha256": hashlib.sha256(PROGRAM.encode()).hexdigest(),
                        "approved_argv": approved_argv, "input_path": str(blob), "input_sha256": hashlib.sha256(BINARY).hexdigest()}
                    _save(evidence / "engineering-launch-contract.json", row["guard_contract"])
                    approved = dict(pending.get("approved_script") or {}).get("path")
                    if not approved:
                        current_attempts = [item for item in durable["provenance_state"]["attempts"].values()
                                            if item["action_id"] == pending["action_id"]]
                        assert len(current_attempts) == 1, current_attempts
                        approved = dict(current_attempts[0].get("approved_script") or {}).get("path")
                    assert approved and Path(approved).is_file(), "use the actual durable approved script reference"
                    authorize = getattr(builtins, "biomind_offline_authorize_diagnostic", None)
                    assert callable(authorize), "this test requires the narrow diagnostic candidate bootstrap"
                    authorize(wrapper=str(path), approved=str(approved), program=str(program),
                              input_path=str(blob), approved_code=probe_code)
                    engineering_kwargs = dict(kwargs)
                    engineering_kwargs["env"] = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
                    answer = await real_runner(path, lifecycle_callback=lifecycle_callback, **engineering_kwargs)
                    row["actual_result"] = answer
                    _save(evidence / "engineering-process-result.json", row)
                    assert answer[2] == 0, answer
                    assert json.loads(answer[0])["probe_valid"] is True
                    return answer
                ordinal = len([item for item in dispatches if not item["diagnostic"]])
                if lifecycle_callback:
                    await lifecycle_callback("PROCESS_STARTED", {"pid": 1, "pgid": 1})
                rc = 1 if ordinal == 1 else 0
                if lifecycle_callback:
                    await lifecycle_callback("PROCESS_EXITED", {"returncode": rc})
                stdout = "BIOMIND_EFFECT_EVIDENCE_JSON=" + json.dumps({"produces_observations": [{"observation_id": "contigs_compatible", "value": rc == 0}]}) + "\n"
                return stdout, "CONTIG_MISMATCH_REASON=generated extractor needs repair" if rc else "", rc
            with patch.object(bio_agents, "run_script_path", new=selective_runner), patch.object(fixture.provider, "ainvoke", new=provider):
                next_input = state
                reached_next = False
                for _ in range(5):
                    try:
                        await graph.ainvoke(next_input, config)
                    except asyncio.CancelledError:
                        if not next_request_seen:
                            raise
                        reached_next = True
                        break
                    snapshot = graph.get_state(config)
                    assert snapshot.tasks and snapshot.tasks[0].interrupts, json.dumps(snapshot.values, default=str)[:3000]
                    approvals.append(deepcopy(snapshot.tasks[0].interrupts[0].value))
                    next_input = Command(resume="y")
            assert reached_next, events
            durable = load_plan_state(fixture.root, task_id)
            receipts = durable["task_state"]["typed_recovery_receipts"]
            probe_receipt = receipts["inspect-binary-header"]
            repair_receipt = receipts["repair-primary-after-diagnostic"]
            assert len(approvals) == 3
            assert [row["diagnostic"] for row in dispatches] == [False, True, False]
            assert len({row["action_id"] for row in dispatches}) == 3
            attempts = list(durable["provenance_state"]["attempts"].values())
            assert len(attempts) == 3 and len({row["attempt_id"] for row in attempts}) == 3
            assert probe_receipt["incident_id"] == repair_receipt["incident_id"] == model.incident_id
            assert durable["task_state"]["typed_recovery_budget"] == {model.incident_id: 2}
            prior, latest = probe_receipt["business_resume_state"], repair_receipt["business_resume_state"]
            assert prior["turn_count"] >= 2 and prior["tool_call_count"] >= 1
            assert latest["turn_count"] >= prior["turn_count"] + 2
            assert latest["tool_call_count"] >= prior["tool_call_count"] + 1
            assert len(model.requests) == 4 and model.probe_result
            assert len(fixture.provider.calls) == 4
            committed = [row for row in events if row["node"] == "commit_step"]
            assert len(committed) == 1
            assert committed[0]["result"]["commit_step_outcome"]["type"] == "next_step_ready"
            assert durable["task_state"]["typed_step_commit_receipts"]
            assert durable["steps"][0]["status"] in {"done", "passed", "completed"}
            assert not durable["task_state"].get("managed_run_stop")
            _save(evidence / "acceptance.json", {"status": "passed", "dispatches": dispatches,
                "approvals": approvals, "recovery_requests": len(model.requests), "generate_requests": len(fixture.provider.calls),
                "probe_result": model.probe_result, "original_incident": model.incident_id,
                "business_budget_before": prior, "business_budget_after": latest, "commits": len(committed)})
        finally:
            if model:
                _save(evidence / "recovery-state-before-teardown.json", {"phase": model.phase, "post_probe": model.post_probe_durable})
            path = fixture.root / "task_outputs" / task_id / "plan_state.json"
            if path.exists():
                _save(evidence / "durable-before-teardown.json", json.loads(path.read_text()))
            _save(evidence / "generate-requests.json", fixture.provider.calls)
            _save(evidence / "dispatches.json", dispatches)
            _save(evidence / "approvals.json", approvals)
            await fixture.asyncTearDown()
    asyncio.run(asyncio.wait_for(scenario(), timeout=75))
