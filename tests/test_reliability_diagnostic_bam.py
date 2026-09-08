"""Real selected samtools header reader over a synthetic, alignment-free BAM.

Only the explicitly pinned ``samtools view --no-PG -H`` may cross the guarded
process boundary. No alignment, variant calling, original input or API is used.
"""
from __future__ import annotations

import asyncio
import builtins
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import re
import shlex
import struct
from unittest.mock import patch
import zlib

import yaml
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import bio_agents
from agent_core.plan_tracker import load_plan_state
from agent_core.skill_access import SkillAccessService
from agent_core.skill_manager import SkillManager
from agent_core.workflow.managed_stop import current_managed_stop
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from agent_core.workflow.scoped_context import provider_visible_payload
from execution.profiling import RunProfiler
from tests import test_reliability_diagnostic_workflow as base
from tests import test_reliability_diagnostic_boundaries as boundary
from tests.test_common_finalization_workflow import SummaryModel


SAMTOOLS = Path("/public/home/liusiqi/anaconda3/envs/biomind/bin/samtools")
SAMTOOLS_SHA256 = "479ed09091628233be90bdd5132f6bc9f299342518bbd40c6106a0ceb63567ef"
HEADER = "@HD\tVN:1.6\tSO:unknown\n@SQ\tSN:chrTiny\tLN:8\n"
OPERATION = "synthetic_bam_header_only"


def _bam_bytes():
    header = HEADER.encode()
    raw = (b"BAM\x01" + struct.pack("<i", len(header)) + header + struct.pack("<i", 1)
           + struct.pack("<i", 8) + b"chrTiny\x00" + struct.pack("<i", 8))
    compressor = zlib.compressobj(level=6, wbits=-15)
    body = compressor.compress(raw) + compressor.flush()
    size = 18 + len(body) + 8
    block = (b"\x1f\x8b\x08\x04" + struct.pack("<I", 0) + b"\x00\xff"
             + struct.pack("<H", 6) + b"BC" + struct.pack("<H", 2) + struct.pack("<H", size - 1)
             + body + struct.pack("<II", zlib.crc32(raw) & 0xffffffff, len(raw)))
    data = block + bytes.fromhex("1f8b08040000000000ff0600424302001b0003000000000000000000")
    assert gzip.decompress(data) == raw
    assert len(data) == 115 and hashlib.sha256(data).hexdigest() == "42db11ef07e53b8e5aa9321b608e8a8342c1cc91048374269763f0939251e7e4"
    return data


def _prepare_before_selection(fixture):
    program, _, _ = base._prepare_skill_before_selection(fixture)
    skill = program.parent.parent / "SKILL.md"
    text = skill.read_text()
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\n|$)", text, re.S)
    metadata = yaml.safe_load(match.group(1))
    generate = metadata["metadata"]["generate"]
    generate["diagnostic_operations"] = [{"operation_id": OPERATION, "read_only": True,
        "argv": [str(SAMTOOLS), "view", "--no-PG", "-H", "{input:0}"],
        "limits": {"max_wall_seconds": 5, "max_input_bytes": 65536, "max_output_bytes": 4096,
                   "max_threads": 1, "max_files": 1},
        "description": "Read only the SAM header of one synthetic BAM. This cannot establish alignment or variant validity."}]
    generate["execution_capabilities"].append({
        "capability_id": "synthetic.diagnostic_boundary.write_reports", "operation": "produce_files",
        "required_runtime_binding_ids": [], "optional_runtime_binding_ids": [], "required_artifact_binding_ids": [],
        "consumes_artifacts": [], "produces_artifacts": ["report", "reproduce_script", "stage_status"],
        "produces_bindings": [], "produces_observations": [], "workspace_mutations": [], "explicit_side_effects": [],
        "execution_guidance": ["Write only the requested evidence reports; a diagnostic result cannot complete the primary step."]})
    skill.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True) + "---\n" + text[match.end():])
    bam = fixture.root / "input-data" / "header-only.synthetic.bam"
    bam.write_bytes(_bam_bytes())
    fixture.path_by_handle["input.diagnostic_blob"] = bam
    fixture.plan_fixture["task_inputs"][-1]["format"] = "bam"
    fixture.manager = SkillManager(str(fixture.root / "selected-skills"))
    fixture.service = SkillAccessService(fixture.manager)
    fixture.agent.skill_manager, fixture.agent.skill_access_service = fixture.manager, fixture.service
    return bam, [str(SAMTOOLS), "view", "--no-PG", "-H", str(bam)]


class _BamRecovery:
    def __init__(self, fixture, task, evidence):
        self.fixture, self.task, self.evidence = fixture, task, evidence
        self.requests, self.incident, self.result = [], None, None

    async def ainvoke(self, messages, **_kwargs):
        self.requests.append([provider_visible_payload(message) for message in messages])
        base._save(self.evidence / ("recovery-request-%02d.json" % len(self.requests)), self.requests[-1])
        seed = base._tag_json(messages, "recovery_seed")
        packet = seed["failure_packet"]
        self.incident = self.incident or packet["incident_id"]
        assert packet["incident_id"] == self.incident
        common = {"schema_version": 1, "task_id": self.task,
                  "target_step_id": seed["invocation_scope"]["step_id"],
                  "evidence_refs": [packet["source_outcome_ref"]]}
        if len(self.requests) == 1:
            return AIMessage(content=json.dumps({**common, "directive_id": "read-bam-header",
                "action": "run_diagnostic_probe", "reason_code": "BAM_HEADER_REQUIRES_SELECTED_DOMAIN_READER",
                "summary": "Inspect the declared BAM header with the selected read-only domain operation.",
                "instruction": "Return the header as bounded diagnostic evidence to this Recovery incident; do not complete the main step.",
                "diagnostic_operation": base.gen_fixture.SKILL_ID + "#" + OPERATION,
                "diagnostic_inputs": ["artifact:" + base.BLOB_ID]}))
        durable = load_plan_state(self.fixture.root, self.task)
        assert durable["current_step_id"] == "step.check_contigs"
        assert durable["task_state"]["typed_recovery_budget"] == {self.incident: 1}
        assert not durable["task_state"].get("typed_step_commit_receipts")
        if len(self.requests) == 2:
            calls = []
            for kind in ("diagnostic", "execution"):
                rows = [row for row in seed["inspection_manifest"] if row.get("scope", {}).get("object_kind") == kind]
                assert len(rows) == 1
                calls.append({"id": "read-bam-" + kind, "name": "evidence_read", "type": "tool_call",
                              "args": {"evidence_ref": rows[0]["ref_id"]}})
            return AIMessage(content="", tool_calls=calls)
        assert len(self.requests) == 3
        tools = [json.loads(message.content) for message in messages if message.type == "tool"]
        tools = [row for row in tools if row["request_id"] in {"read-bam-diagnostic", "read-bam-execution"}]
        assert len(tools) == 2 and not any(row["error"] for row in tools)
        objects = list(base._json_objects(tools))
        assert any(row.get("status") == "SUCCEEDED" and row.get("exit_code") == 0 for row in objects)
        assert any(row.get("raw_stdout") == HEADER or row.get("stdout") == HEADER for row in objects), tools
        self.result = deepcopy(tools)
        base._save(self.evidence / "actual-bam-header-in-next-request.json", tools)
        return AIMessage(content=json.dumps({**common, "directive_id": "stop-after-bam-header", "action": "blocked",
            "reason_code": "DIAGNOSTIC_BAM_HEADER_ONLY", "summary": "The bounded reader returned the synthetic BAM header, chrTiny length 8. The original primary execution failure remains unresolved; header readability does not establish alignment or variant validity."}))


def test_real_samtools_header_diagnostic_returns_evidence_and_truthful_public_stop(tmp_path):
    async def scenario():
        fixture = base.gen_fixture.Session1788681718GenerateProductionPathTests(methodName="test_adm_01_saved_candidate_reaches_fake_executor_once")
        await fixture.asyncSetUp()
        task, evidence = "diagnostic-bam-header", tmp_path / "evidence"
        events, approvals, dispatches = [], [], []
        model = summary = None
        try:
            bam, argv = _prepare_before_selection(fixture)
            assert hashlib.sha256(SAMTOOLS.read_bytes()).hexdigest() == SAMTOOLS_SHA256
            before = hashlib.sha256(bam.read_bytes()).hexdigest()
            state = await boundary._approve_with_scoped_report_usage(fixture, task, evidence)
            base._save(evidence / "initial-approved-state.json", load_plan_state(fixture.root, task))
            first = json.loads(fixture.saved_response_content.strip().removeprefix("```json").removesuffix("```"))
            code = shlex.join(argv)
            fixture.provider.responses = [json.dumps(first), json.dumps({"type": "execute", "language": "bash",
                "purpose": "probe", "operation": "check_contig_compatibility", "argv": argv, "code": code, "env": {}, "outputs": []})]
            fixture.provider.calls.clear()
            fixture.agent.middleware.auto_approve_safe = False
            model = _BamRecovery(fixture, task, evidence)
            recovery = Schema2RecoveryNode(data_path=str(fixture.root), model=model,
                phase_store=fixture.agent.phase_archive_store, skill_service=fixture.service)
            handlers = {name: fixture.agent._schema2_runtime_node_handlers[name]
                        for name in ("generate", "execute", "supervisor", "commit_step", "final_audit")}
            handlers["recovery"] = recovery.run
            def observed(name, handler):
                async def invoke(node_state):
                    result = await handler(node_state)
                    events.append({"node": name, "result": deepcopy(result)})
                    base._save(evidence / "graph-events.json", events)
                    return result
                return invoke
            graph = build_schema2_runtime_graph({name: observed(name, handler) for name, handler in handlers.items()}).compile(checkpointer=InMemorySaver())
            config = {"configurable": {"thread_id": task}, "recursion_limit": 25}
            real_runner = bio_agents.run_script_path
            async def runner(path, *, lifecycle_callback=None, **kwargs):
                durable = load_plan_state(fixture.root, task)
                pending = durable["task_state"]["pending_action"]
                diagnostic = (pending.get("recovery_execution_envelope") or {}).get("result_contract") == "DIAGNOSTIC_EVIDENCE"
                row = {"diagnostic": diagnostic, "action_id": pending["action_id"], "lifecycle": []}
                dispatches.append(row)
                if not diagnostic:
                    assert len(dispatches) == 1
                    if lifecycle_callback:
                        await lifecycle_callback("PROCESS_STARTED", {"pid": 1, "pgid": 1})
                        await lifecycle_callback("PROCESS_EXITED", {"returncode": 1})
                    return "", "Synthetic primary failure; domain validity has not been established.", 1
                assert len(dispatches) == 2 and hashlib.sha256(bam.read_bytes()).hexdigest() == before
                attempts = [item for item in durable["provenance_state"]["attempts"].values() if item["action_id"] == pending["action_id"]]
                assert len(attempts) == 1
                approved = attempts[0]["approved_script"]["path"]
                authorize = getattr(builtins, "biomind_offline_authorize_samtools_header", None)
                assert callable(authorize), "Use the separately pinned samtools diagnostic test driver."
                authorize(wrapper=str(path), approved=approved, program=str(SAMTOOLS), input_path=str(bam), approved_code=code)
                async def lifecycle(event, details):
                    row["lifecycle"].append({"event": event, "details": deepcopy(details)})
                    if lifecycle_callback:
                        await lifecycle_callback(event, details)
                result = await real_runner(path, lifecycle_callback=lifecycle,
                    **{**kwargs, "env": {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}})
                row["actual_result"] = result
                base._save(evidence / "real-samtools-execution.json", row)
                assert result[2] == 0 and result[0] == HEADER, result
                return result
            summary = SummaryModel("ok")
            fixture.agent.terminal_summary_llm = summary
            fixture.agent.terminal_summary_policy = {"enabled": True, "timeout_seconds": 1}
            fixture.agent.llm_timeout_seconds = fixture.agent.llm_readonly_timeout_seconds = fixture.agent.llm_total_timeout_seconds = 1
            fixture.agent.state_manager = None
            fixture.agent.profiler = RunProfiler(fixture.root / "task_outputs")
            fixture.agent.profiler.start_task(task)
            fixture.agent._app_for_task = lambda _task: graph
            async def go(_text, **_kwargs):
                value = state
                for _ in range(3):
                    await graph.ainvoke(value, config)
                    snapshot = graph.get_state(config)
                    if not snapshot.tasks:
                        return
                    assert snapshot.tasks[0].interrupts
                    approvals.append(deepcopy(snapshot.tasks[0].interrupts[0].value))
                    value = Command(resume="y")
                raise AssertionError("unexpected extra approval")
            fixture.agent.go = go
            with patch.object(bio_agents, "run_script_path", new=runner):
                outer = await fixture.agent._run_agent_interrupt_loop("isolated BAM header diagnostic", task)
            durable = load_plan_state(fixture.root, task)
            stop = current_managed_stop(durable)
            assert model.result and len(model.requests) == 3 and len(fixture.provider.calls) == 2
            assert len(approvals) == len(dispatches) == len(durable["provenance_state"]["attempts"]) == 2
            assert [row["diagnostic"] for row in dispatches] == [False, True]
            assert not any(row["node"] in {"supervisor", "commit_step"} for row in events)
            assert not durable["task_state"].get("typed_step_commit_receipts")
            assert durable["current_step_id"] == "step.check_contigs"
            assert stop["stop_reason"]["error_code"] == outer.failure_code == "DIAGNOSTIC_BAM_HEADER_ONLY"
            assert stop["origin"]["returncode"] == 1 and stop["execution"]["returncode"] == 0
            assert stop["execution"]["purpose"] == "diagnostic" and not stop["execution"]["committed"]
            assert stop["summary_status"] == "COMPLETED" and len(summary.requests) == stop["summary_llm_calls"] == 1
            facts = json.loads(summary.requests[0][1].content)
            assert facts["origin"]["returncode"] == 1 and facts["execution"]["returncode"] == 0
            reports = {}
            assert {"report.md", "stage_status.tsv", "reproduce.sh"} <= set(stop["report_paths"])
            for name, path in stop["report_paths"].items():
                report = Path(path)
                assert report.is_relative_to(fixture.root) and report.is_file()
                preserved = evidence / "reports" / name
                preserved.parent.mkdir(parents=True, exist_ok=True)
                preserved.write_bytes(report.read_bytes())
                reports[name] = {"path": path, "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                                 "preserved_path": str(preserved), "receipt": stop["report_receipts"][name]}
            reproduce = Path(reports["reproduce.sh"]["preserved_path"]).read_text()
            assert code in reproduce and stop["origin"]["attempt_id"] in reproduce
            assert "Latest recorded action only; not a complete workflow replay" in reproduce
            assert "其返回码不代表主步骤完成" in Path(reports["report.md"]["preserved_path"]).read_text()
            assert "latest attempt purpose=diagnostic" in Path(reports["stage_status.tsv"]["preserved_path"]).read_text()
            assert hashlib.sha256(bam.read_bytes()).hexdigest() == before
            base._save(evidence / "public-stop-and-reports.json", {"stop": stop, "reports": reports, "actual_summary_facts": facts})
            base._save(evidence / "acceptance.json", {"status": "passed", "real_operation": argv,
                "samtools_sha256": SAMTOOLS_SHA256, "input_sha256": before, "input_unchanged": True,
                "bam_alignment_records": 0, "header": HEADER, "generate_calls": 2, "recovery_calls": 3,
                "approvals": 2, "main_step_commits": 0, "summary_calls": 1, "reports": reports,
                "scientific_alignment_and_calling": "NOT_RUN"})
        finally:
            path = fixture.root / "task_outputs" / task / "plan_state.json"
            if path.exists():
                base._save(evidence / "durable-before-teardown.json", json.loads(path.read_text()))
            base._save(evidence / "generate-requests.json", fixture.provider.calls)
            base._save(evidence / "dispatches.json", dispatches)
            base._save(evidence / "approvals.json", approvals)
            if summary is not None:
                base._save(evidence / "summary-requests.json", [[provider_visible_payload(message) for message in request] for request in summary.requests])
            await fixture.asyncTearDown()
    asyncio.run(asyncio.wait_for(scenario(), timeout=75))
