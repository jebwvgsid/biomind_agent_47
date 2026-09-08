"""Recorded WGS preflight recovery with real inspection and admission machinery.

Scientific process dispatch is the controlled boundary: the first result is a
recorded rc0/value-only shape or its canonical false-claim variant, and the
second executes only bounded engineering readers over four tiny temporary gzip
fixtures. Tool availability is explicitly
a synthetic fixture fact. No scientific binary, original input or API is used.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import redirect_stdout
from copy import deepcopy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import shlex
import sys
from unittest.mock import patch
import zlib

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import bio_agents
from agent_core.approved_resource_locator import open_approved_resource
from agent_core.inspection_readers import inspect_stream, ReadLimits
from agent_core.plan_tracker import load_plan_state
from agent_core.state_contract_store import StateContractStore
from agent_core.step_effects import emit_observation, extract_structured_effect_evidence
from agent_core.workflow.early_nodes import TypedPlanner, TypedPlanReview
from agent_core.workflow.inspection_evidence import read_execution_facts
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder, bind_causal_failure_node
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from agent_core.workflow.scoped_context import provider_visible_payload
from agent_core.workflow.verifier import TypedSupervisor
from supervisor import SupervisorNode
from tests.planner_fix_acceptance import test_planner_recorded_workflow as shared
from tests.planner_truncation_acceptance.harness import Model
from tests.test_inspection_workflow import InspectionReviewer, _tag_json
from tests.test_reliability_planner_consumption import _BASIS, _recording
from agent_core.plan_contract import parse_planner_outcome_envelope


_ORIGINAL_ACTION_B85 = 'c$~FY&2Aev5WeqIOjy@HaHO?ZpPI<1<H&#uRB2_SJyh+2TFq(+ued9cvJ?NDdnk&Y@*2&lmmc~y4br#hkgK(%wPmzLbg@a!kn_#QnUAaICdDi}2n<?i;53Iq7MKW`1wpTK-05|?N1f>S@bv7k&B9_j$YdO*`541YPK}(%aPPC``*&|Y{Pt%!KozHBq0ddY*J{51{pSzw-iCYo&A)&8g(iRf@oV_C(6-gQ5LyalqgZ5_RHJYxtO!S_=+tH+j1??6ZAHD-dIM63Rx_D2M^|MyzV>kZE*xKfc6=c_zW(g^JiFi%XfzsdqKc^nd@YTolM#d(g346`$Ay&&jd5N<l50pnDqtJ6a$~gQ%b*%?Kj77hONu>%VIBa;1iptS&@g0382s=IY>J8u02rq^uoow<zB=i?j5^21oujj;^X<t?AL+}}SKan?pIwM-ihcGu4fAvQpNP!h(>_D3b4~L;8%tv--6+?Q%iv8UU4DH=PKB_k_t{AL?Dm$GabHUddo)aBFw7NJw7A^9fzg#LC~J!&E$ti6jYctqLP$+fzzF5m?--ht6#WN2o|G{f)=@HHzz@P9UW96zWouwZD)N!T$i=L4GI^YoqZH$FKoO@iZc<@DSjdp10LZCiCZ#{9JyV3fT#NImvM?4lPSL;s?Ioh}9GW0@$3{+0Cfhr3O);~aN|vZ>vyB$3NV<x}0}R29j|U9}`!pVDBqk@Y_2U;?YambMkxR!!3FR|<gg%XSsui?K0&f>{oEOK<O-1v4Gk?U)AK%SuyWP!s`aN`4p8g6GnIUZNL*`l>3c4|8-wFIxm1(ZHj+HkdF3(}B+k;Q(F4}Eq!L{1my@gKqC0rNOEf&=7-t89g;Ted!?Tzh)aCgk?lrq1<&YhjF*w1JC`HKC#+#h%yn#GH{jOqug<K1+wOCPIr=x7Hbay1{7ph&2ENBBC@Epn^yxH+%p1gI>l*?m$$Rp|LLdcHpN{9mExHT3*J^fP_!VS?H}IQ|v!<tGL*tK_tl#=C|-*m~gQ`RIx23RFbBZg4VnV^iz&8j7mz?`3$F=NX6#`b1mIt9qX-SxuWU|8BU6YfU4@ejNsJQVvGKp3|pZEc9^k&4}~y*hy^Tf@pD`3>JEkA8fW3^sg3jV`&cM_GPVq@!cU1tih#djB(6i8zr5lL6YLfF&0(~GTaI3P{_Xy12#AB`IKj_!uvQ{+8$O%Fi*DchK74}mps?e2TK8bRm@h(!l;>7l*J(>>@}NYR~UmsUl#ulqSaG}(0}5XsL*Io+4;JE6e9oP&ll%4Ubb9@bDKvV&1!bh;M?DKzA3|dsan=7uT~4P_0$VTuci%N6_(_IXnG!&zi$2kcwsHX'
_ORIGINAL_ACTION_SHA256 = '8a358ded32887d60a9b1a166381be3f1232ff038a5021d3e2c7401d448f40984'
_TOOLS = {name: True for name in ("bwa", "samtools", "gatk", "bcftools", "picard")}


def _save(directory, name, value):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / (name + ".json")).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )


def _recorded_action():
    raw = zlib.decompress(base64.b85decode(_ORIGINAL_ACTION_B85)).decode()
    assert hashlib.sha256(raw.encode()).hexdigest() == _ORIGINAL_ACTION_SHA256
    assert "head -n 4 | grep -q '^@'" in raw
    return raw


def _bootstrap_wgs(harness, task_id, directory):
    record = _recording()
    task = deepcopy(record["task_spec"])
    paths = {}
    replacements = {}
    directory.mkdir(parents=True)
    for item in task["inputs"]:
        path = directory / item["required_basename"]
        if item["role"] == "reference_fasta":
            path.write_text(">chr1\nACGTACGT\n")
        else:
            instance = item["input_instance"]
            group, mate = instance["group_id"], instance["member_id"][-1]
            payload = "".join(f"@{group}-read-{n}/{mate}\nACGT\n+\nIIII\n" for n in range(2))
            path.write_bytes(gzip.compress(payload.encode(), mtime=0))
        replacements[item["canonical_path"]] = str(path)
        item["canonical_path"] = str(path)
        item["resolution_base"] = str(directory)
        paths[item["semantic_handle"]] = path
    # Synthetic approved TaskSpec keeps every original scientific requirement.
    # Only concrete input/output locations and local submission identity change.
    def relocate(value):
        if isinstance(value, str):
            return value.replace("/public/home/liusiqi/03.毕设", str(harness.root / "relocated"))
        if isinstance(value, list):
            return [relocate(row) for row in value]
        if isinstance(value, dict):
            return {key: relocate(row) for key, row in value.items()}
        return value
    task = relocate(task)
    fixture = json.loads((shared.F / "recorded_state.json").read_text())
    fixture["current_task"]["task_spec"] = task
    fixture_dir = harness.root / "wgs-handoff-fixture"
    fixture_dir.mkdir()
    (fixture_dir / "recorded_state.json").write_text(json.dumps(fixture, ensure_ascii=False))
    with patch.object(shared, "F", fixture_dir):
        initial = harness.bootstrap(task_id)
    original = _recorded_action()
    for before, after in replacements.items():
        assert original.count(before) == 1
        original = original.replace(before, after)
    return initial, paths, original


class _WgsRecoveryModel:
    def __init__(self, root, task, evidence, first_claim, graph_events):
        self.root, self.task, self.evidence = root, task, evidence
        self.first_claim = first_claim
        self.graph_events = graph_events
        self.requests, self.read_results = [], []
        self.incident = None

    async def ainvoke(self, messages, **_kwargs):
        wire = [provider_visible_payload(message) for message in messages]
        self.requests.append(wire)
        _save(self.evidence, "recovery-request-%02d" % len(self.requests), wire)
        seed = _tag_json(list(messages), "recovery_seed")
        packet = seed["failure_packet"]
        self.incident = self.incident or packet["incident_id"]
        assert self.incident == packet["incident_id"]
        assert seed["invocation_scope"]["step_id"] == "preflight_inputs"
        durable = load_plan_state(self.root, self.task)
        assert not durable["task_state"].get("typed_step_commit_receipts")
        if len(self.requests) == 1:
            # Oracle: the real Execute outcome and its actual durable facts.
            # Reading them here verifies provider visibility; they are never
            # inserted into the model request by the test.
            record = next(row["result"]["execute_outcome"]["payload"]["execution_record"]
                          for row in self.graph_events if row["node"] == "execute")
            assert record["attempt_id"] == packet["failed_attempt_ref"]
            held = read_execution_facts(StateContractStore(self.root, self.task),
                                        record["recovery_execution_facts_ref"])
            actual_materials, visibility = {}, []
            for index, message in enumerate(messages):
                text = str(message.content)
                if '<inherited_recovery_evidence ' not in text:
                    continue
                descriptor = json.loads(text.split("\n", 1)[1].split("\n---BEGIN EXACT SOURCE BODY---", 1)[0])
                body = text.split("---BEGIN EXACT SOURCE BODY---\n", 1)[1].split("\n---END EXACT SOURCE BODY---", 1)[0]
                kind = descriptor["object_kind"]
                actual_materials[kind] = json.loads(body)
                visibility.append({"object_kind": kind, "pointer": f"recovery-request-01.json#/{index}/content",
                    "body_chars": len(body), "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                    "descriptor": descriptor})
            script = actual_materials["script"]["scripts"]
            for key in ("code", "generated_code", "execution_wrapper"):
                assert held[key] and script[key] == held[key], f"The actual Recovery request omitted or changed {key}."
                assert script["availability"][key] == "recorded"
            assert script["identities"] == held["script_identities"]
            assert script["argv"] == held["argv"]
            normalized = durable["provenance_state"]["actions"][record["action_id"]]["normalized_action"]
            action = actual_materials["action"]["action"]
            for field in ("action_id", "plan_id", "plan_version", "step_id"):
                assert field in normalized and action[field] == normalized[field]
            assert action["expected_step_effect"] == normalized["expected_step_effect"]
            source_report = next(row["result"]["verification_outcome"]["payload"]["completion_report"]
                                 for row in self.graph_events if row["node"] == "supervisor")
            assert actual_materials["completion_report"] == source_report
            assert actual_materials["execution"]["execution"]["raw_stdout"] == held["raw_stdout"]
            _save(self.evidence, "recovery-core-visibility", visibility)
            inputs = [row for row in seed["inspection_manifest"] if row.get("kind") == "artifact"
                      and row.get("scope", {}).get("direction") == "input"]
            assert len(inputs) == 5, inputs
            paired_ids = {item["artifact_id"] for item in durable["current_task"]["task_spec"]["inputs"]
                          if item["role"] == "paired_fastq"}
            calls = []
            for entry in inputs:
                if entry["ref_id"].removeprefix("artifact:") in paired_ids:
                    calls.append({"id": "fastq-%d" % len(calls), "name": "artifact_read", "type": "tool_call",
                                  "args": {"ref_id": entry["ref_id"], "representation": "fastq_prefix",
                                           "start_line": 1, "end_line": 8, "max_bytes": 4096}})
            assert len(calls) == 4
            return AIMessage(content="", tool_calls=calls)
        assert len(self.requests) == 2, "Recovery must make progress after the actual bounded reads."
        results = [json.loads(message.content) for message in messages if message.type == "tool"]
        results = [row for row in results if row.get("request_id", "").startswith("fastq-")]
        assert len(results) == 4, results
        for row in results:
            assert not row["error"], row
            result = row["result"]
            if result.get("status") == "EXTERNALIZED":
                # The current phase representation preserves this complete
                # small object inside its bounded excerpt. Assert completeness
                # before parsing it; an archive ref alone is not model access.
                excerpt = result["bounded_excerpt"]
                assert len(excerpt) == result["observed_chars"]
                assert hashlib.sha256(excerpt.encode()).hexdigest() == result["content_sha256"].removeprefix("sha256:")
                result = json.loads(excerpt)
            inspection = result["inspection"]
            assert inspection["checked_records"] == 2
            assert inspection["eof"] and inspection["full_compression_verified"]
            assert inspection["prefix_valid"] and inspection["full_scientific_validation"] is False
            assert "@S" in result["content"] and "ACGT" in result["content"]
        self.read_results = deepcopy(results)
        return AIMessage(content=json.dumps({
            "schema_version": 1, "directive_id": "wgs-repair-observation-and-reader",
            "task_id": self.task, "target_step_id": "preflight_inputs", "action": "retry_step",
            "reason_code": "GENERATED_ACTION_FAILED",
            "summary": ("The scoped gzip reads contain records. The rc0 claim did not establish no_reads_found. "
                        + ("The original claim also omitted observation identity." if self.first_claim == "value_only"
                           else "The formal observation identity was valid; its adverse scientific claim still required correction.")),
            "instruction": "Replace the zcat/head/grep early-close check with the selected Skill bounded FASTQ reader. Preserve full gzip, mate IDs/counts, FASTA and tool-availability obligations; emit exactly validation_observation using the current emission contract. Prefix coverage alone is not full scientific validation. Produce a new action requiring fresh approval.",
            "evidence_refs": [packet["source_outcome_ref"], *[row["source_ref"] for row in results]],
        }, ensure_ascii=False))


class _WgsScientificReviewer:
    """Offline semantic model; all report IDs and source bodies are real inputs."""

    def __init__(self, evidence):
        self.evidence = evidence
        self.requests, self.reports, self.values = [], [], []

    async def ainvoke(self, messages, **_kwargs):
        self.requests.append([provider_visible_payload(message) for message in messages])
        _save(self.evidence, "supervisor-requests", self.requests)
        report = _tag_json(list(messages), "completion_report")
        self.reports.append(report)
        source = next(str(message.content) for message in messages
                      if '<supervisor_current_source kind="stdout"' in str(message.content))
        body = source.split("---BEGIN CALLER-SUPPLIED SOURCE---\n", 1)[1].split(
            "\n---END CALLER-SUPPLIED SOURCE---", 1)[0]
        rows = extract_structured_effect_evidence(body)["produces_observations"]
        assert len(rows) == 1 and rows[0]["identity"] == "validation_observation"
        value = rows[0]["value"]
        self.values.append(deepcopy(value))
        accepted = value.get("ok") is True
        if not accepted:
            assert value == {"ok": False, "error": "no_reads_found", "path": value["path"]}
        disposition = "completion_accepted" if accepted else "inspection_required"
        reason = ("The current attempt reports the bounded fixture checks with their explicit coverage."
                  if accepted else "The legal observation asserts no_reads_found; inspect the approved FASTQ bytes before accepting that scientific conclusion.")
        _save(self.evidence, "supervisor-requests", self.requests)
        return AIMessage(content=json.dumps({
            "report_id": report["report_id"], "assessment": "ACCEPTABLE" if accepted else "CONCERN",
            "disposition": disposition, "issue_dispositions": [{
                "issue_id": report["scope"]["validation_id"], "disposition": disposition,
                "explanation": reason,
            }], "concerns": [] if accepted else [reason], "suggestions": [], "confidence": 0.9,
        }))


def _formalize_observation_claims(source):
    """Keep the recorded checks, changing only their explicit claim envelope."""
    needle = 'BIOMIND_EFFECT_EVIDENCE_JSON={\\"value\\":'
    replacement = 'BIOMIND_EFFECT_EVIDENCE_JSON={\\"schema_version\\":1,\\"produces_observations\\":[{\\"identity\\":\\"validation_observation\\",\\"value\\":'
    lines = []
    for line in source.splitlines():
        if needle in line:
            assert line.endswith('}"'), line
            line = line.replace(needle, replacement)[:-2] + '}]}"'
        lines.append(line)
    return "\n".join(lines)


def _correction_script(original, helper):
    # This generated source is reviewed/admitted/persisted, never executed here.
    # Keep original full-gzip, read-count, reference and command-availability
    # checks; replace only the early-close probe and formal emission identity.
    needle = "if ! zcat \"$fq\" 2>/dev/null | head -n 4 | grep -q '^@'; then"
    predicate = ('import json,sys; raw=sys.stdin.read(65536); '
                 'rows=json.loads(raw.split("=",1)[1])["produces_observations"]; '
                 'assert len(rows)==1 and rows[0]["identity"]=="validation_observation"; '
                 'assert rows[0]["value"]["prefix_valid"] is True')
    replacement = ("if ! " + shlex.join([sys.executable, str(helper)])
                   + ' --path "$fq" --observation-id validation_observation | '
                   + shlex.join([sys.executable, "-c", predicate]) + '; then')
    assert needle in original
    repaired = original.replace(needle, replacement)
    # The source keeps the same check values; this exact, declared identity is
    # the sole normalization of each emitted observation (including negatives).
    return _formalize_observation_claims(repaired)


@pytest.mark.parametrize("first_claim", ["value_only", "canonical_false"])
def test_wgs_value_only_failure_reads_evidence_and_new_action_commits(tmp_path, first_claim):
    async def scenario():
        scenario_caller = asyncio.current_task()
        task = "offline-wgs-preflight-recovery"
        evidence = Path(os.environ["BIOMIND_OFFLINE_RESULTS"]).parent / "wgs-recovery-evidence" / first_claim
        recorded = _recording()
        plan = deepcopy(dict(parse_planner_outcome_envelope(recorded["candidate2_raw"]).payload))
        plan["steps"][16]["success_criteria"].append(_BASIS)
        model = Model([json.dumps(plan, ensure_ascii=False)])
        h = shared.Harness(tmp_path, model)
        events, dispatches, approvals, next_requests_seen = [], [], [], []
        recovery_model = None
        try:
            initial, paths, original = _bootstrap_wgs(h, task, tmp_path / "wgs-inputs")
            if first_claim == "canonical_false":
                original = _formalize_observation_claims(original)
                assert 'if ! zcat "$fq" 2>/dev/null | head -n 4' in original
                assert '\\"identity\\":\\"validation_observation\\"' in original
                assert 'BIOMIND_EFFECT_EVIDENCE_JSON={\\"value\\":' not in original
            planner = TypedPlanner(data_path=str(tmp_path), source=h.agent._schema2_early_sources["planner"])
            planned = await planner.run(initial)
            assert planned["planner_outcome"]["type"] == "plan_review_ready", planned
            h.human_answers = ["y"]
            review = TypedPlanReview(data_path=str(tmp_path), source=h.agent._schema2_early_sources["plan_review"], execution_backend="legacy")
            reviewed = await review.run({**initial, **planned})
            assert reviewed["plan_review_outcome"]["type"] == "approved_for_execution", reviewed
            approved = load_plan_state(tmp_path, task)
            assert approved["approval_status"] == "approved" and len(approved["steps"]) == 18
            assert approved["steps"][0]["success_criteria"] == plan["steps"][0]["success_criteria"]
            _save(evidence, "approved-plan", approved)
            helper = Path(bio_agents.__file__).parent / "skills/genomics/wgs-short-read-mapping/scripts/inspect_fastq.py"
            corrected = _correction_script(original, helper)

            def request_helper(_model, messages):
                text = "\n".join(str(message.content) for message in messages)
                assert "wgs-repair-observation-and-reader" in text
                assert "Replace the zcat/head/grep early-close check" in text
                assert "observation_emission_contract" in text and "validation_observation" in text
                return json.dumps({"decision": "REQUEST_SKILL_CONTEXT",
                    "reason": "Read the selected Skill's approved FASTQ helper and concrete invocation locator before creating its corrected action.",
                    "requests": [{"skill_id": "genomics/wgs-short-read-mapping", "operation": "resource",
                                  "resource_path": "scripts/inspect_fastq.py"}]})

            def correction_response(_model, messages):
                text = "\n".join(str(message.content) for message in messages)
                source = helper.read_text()
                assert "scripts/inspect_fastq.py" in text
                def contains_source(message):
                    if source.strip() in str(message.content):
                        return True
                    objects = shared.wire_objects([provider_visible_payload(message)])
                    return any(isinstance(value, str) and value.strip() == source.strip() for value in objects)
                assert any(contains_source(message) for message in messages), "The selected script body must reach the actual next Generate request."
                assert str(helper) in text, "The actual Skill response must supply the executable locator; the fake model may not guess it."
                assert "wgs-repair-observation-and-reader" in text
                _save(evidence, "helper-provider-visibility", {
                    "provider_call_ordinal": len(model.calls), "locator": str(helper),
                    "body_sha256": hashlib.sha256(source.encode()).hexdigest(),
                    "material_pointers": [f"provider-requests.json#/{len(model.calls)-1}/{i}/content"
                        for i, message in enumerate(messages) if str(helper) in str(message.content)
                        or contains_source(message)],
                    "request_sha256": hashlib.sha256(text.encode()).hexdigest(),
                })
                return corrected

            async def next_response(_model, _messages):
                current = load_plan_state(tmp_path, task)
                assert current["current_step_id"] == plan["steps"][1]["step_id"]
                assert current["task_state"]["typed_step_commit_receipts"]
                next_requests_seen.append({"provider_call_ordinal": len(model.calls),
                    "step_id": current["current_step_id"], "commit_receipts": deepcopy(current["task_state"]["typed_step_commit_receipts"]),
                    "boundary_stop": "test intentionally cancels its scenario caller after capturing the actual next request; not a user cancellation"})
                _save(evidence, "next-step-request", next_requests_seen)
                scenario_caller.cancel("isolated test reached the next WGS step request")
                await asyncio.Future()

            model.responses.extend([original, request_helper, correction_response, next_response])
            h.agent.middleware.auto_approve_safe = False
            recovery_model = _WgsRecoveryModel(tmp_path, task, evidence, first_claim, events)
            recovery = Schema2RecoveryNode(data_path=str(tmp_path), model=recovery_model,
                phase_store=h.agent.phase_archive_store, skill_service=h.service)
            reviewer = _WgsScientificReviewer(evidence) if first_claim == "canonical_false" else InspectionReviewer(accept_first=True)
            supervisor = bind_causal_failure_node(TypedSupervisor(data_path=str(tmp_path),
                verifier=SupervisorNode(reviewer, output_dir=str(tmp_path / "task_outputs")).run).run,
                binder=CausalFailurePacketBinder(data_path=str(tmp_path)), outcome_field="verification_outcome")
            handlers = h.agent._schema2_runtime_node_handlers
            configured = {**{key: handlers[key] for key in ("generate", "execute", "commit_step", "final_audit")},
                          "supervisor": supervisor, "recovery": recovery.run}
            def wrap(name, handler):
                async def invoke(state):
                    result = await handler(state)
                    events.append({"node": name, "result": deepcopy(result)})
                    _save(evidence, "graph-events", events)
                    return result
                return invoke
            graph = build_schema2_runtime_graph({name: wrap(name, handler) for name, handler in configured.items()}).compile(checkpointer=InMemorySaver())
            config = {"configurable": {"thread_id": task}, "recursion_limit": 35}
            paired = [row for row in approved["current_task"]["task_spec"]["inputs"] if row["role"] == "paired_fastq"]

            async def fake_scientific_dispatch(path, *, lifecycle_callback=None, **kwargs):
                durable = load_plan_state(tmp_path, task)
                pending = durable["task_state"]["pending_action"]
                dispatches.append({"script_path": str(path), "action_id": pending["action_id"], "expected_sha256": kwargs.get("expected_sha256")})
                if lifecycle_callback:
                    await lifecycle_callback("PROCESS_STARTED", {"pid": 1, "pgid": 1})
                if len(dispatches) == 1:
                    # Record the real incident shape without executing its
                    # known unsafe pipeline or any external scientific binary.
                    adverse = {
                        "ok": False, "error": "no_reads_found", "path": str(paths[paired[0]["semantic_handle"]]),
                    }
                    stdout = ((emit_observation("validation_observation", adverse) if first_claim == "canonical_false"
                               else "BIOMIND_EFFECT_EVIDENCE_JSON=" + json.dumps({"value": adverse})) + "\n")
                else:
                    assert len(dispatches) == 2
                    main = runpy.run_path(str(helper))["main"]
                    members, names = {}, {}
                    for row in paired:
                        p = paths[row["semantic_handle"]]
                        captured = io.StringIO()
                        with redirect_stdout(captured):
                            rc = main(["--path", str(p), "--observation-id", "validation_observation",
                                       "--records", "32", "--input-bytes", "4096", "--decoded-bytes", "4096"])
                        assert rc == 0
                        parsed = extract_structured_effect_evidence(captured.getvalue())
                        observation = parsed["produces_observations"][0]
                        assert observation["identity"] == "validation_observation"
                        value = observation["value"]
                        assert value["eof"] and value["full_compression_verified"] and value["checked_records"] == 2
                        assert value["full_scientific_validation"] is False
                        with open_approved_resource(p) as handle:
                            content = inspect_stream(handle, representation="fastq_prefix", limits=ReadLimits(input_bytes=4096, decoded_bytes=4096))["content"]
                        ids = [line.rsplit("/", 1)[0] for line in content.splitlines()[::4]]
                        instance = row["input_instance"]
                        names[(instance["group_id"], instance["member_id"])] = ids
                        members[row["semantic_handle"]] = value
                    assert names[("S1", "R1")] == names[("S1", "R2")]
                    assert names[("S2", "R1")] == names[("S2", "R2")]
                    with open_approved_resource(paths["input.reference_fasta"]) as handle:
                        reference = inspect_stream(handle, representation="text", limits=ReadLimits(input_bytes=4096, decoded_bytes=4096))
                    assert reference["eof"] and reference["content"].startswith(">chr1\n")
                    value = {"ok": True, "inputs_present": True, "fastq_valid": True,
                        "paired_fixture_ids_checked": True, "reference_readable": True,
                        "tools_available": True, "tool_availability_evidence": {
                            "source": "explicit offline controlled fixture; no scientific executable inspected or launched", "tools": _TOOLS},
                        "members": members, "full_scientific_validation": False,
                        "coverage": "all records of four two-record synthetic inputs; WGS science remains outside this test"}
                    stdout = emit_observation("validation_observation", value) + "\n"
                    dispatches[-1]["reader_emitter_result"] = value
                dispatches[-1].update(returncode=0, stdout=stdout, stderr="")
                _save(evidence, "dispatches", dispatches)
                if lifecycle_callback:
                    await lifecycle_callback("PROCESS_EXITED", {"returncode": 0})
                return stdout, "", 0

            runtime_state = {**initial, **planned, **reviewed, "current_step_id": "preflight_inputs", "task_status": "running"}
            if first_claim == "canonical_false":
                runtime_state["validation_level"] = "scientific"
            runtime_state.pop("resume_state", None)
            reached_next = False
            with patch.object(bio_agents, "run_script_path", new=fake_scientific_dispatch):
                next_input = runtime_state
                for _ in range(4):
                    try:
                        await graph.ainvoke(next_input, config)
                    except asyncio.CancelledError:
                        if not next_requests_seen:
                            raise
                        reached_next = True
                        break
                    if next_requests_seen:
                        # Provider lifecycle may consume its own cancellation.
                        # The actual next request and prior Commit are the
                        # acceptance boundary, not a synthetic exception type.
                        reached_next = True
                        break
                    snap = graph.get_state(config)
                    assert snap.tasks and snap.tasks[0].interrupts, json.dumps(snap.values, default=str)[:4000]
                    approvals.append(deepcopy(snap.tasks[0].interrupts[0].value))
                    next_input = Command(resume="y")
            durable = load_plan_state(tmp_path, task)
            _save(evidence, "final-state", durable)
            assert reached_next, events
            assert len(approvals) == 2 and len(dispatches) == 2
            assert len({row["action_id"] for row in dispatches}) == 2
            attempts = list(durable["provenance_state"]["attempts"].values())
            assert len(attempts) == 2 and len({row["attempt_id"] for row in attempts}) == 2
            assert len(recovery_model.requests) == 2 and len(recovery_model.read_results) == 4
            assert len(model.calls) == 5  # Planner, original, helper request, correction, next step.
            verifies = [row["result"]["verification_outcome"] for row in events if row["node"] == "supervisor"]
            assert verifies[0]["type"] in {"rejected", "incomplete_evidence", "inspection_required"}, verifies
            if first_claim == "value_only":
                assert "identity" in json.dumps(verifies[0], ensure_ascii=False).lower(), verifies[0]
            else:
                assert len(reviewer.requests) == 2 and reviewer.values[0]["ok"] is False
                assert reviewer.values[1]["ok"] is True
                assert reviewer.reports[0]["completion_gate"]["passed"] is True
                assert "no_reads_found" in json.dumps(verifies[0], ensure_ascii=False), verifies[0]
            assert verifies[-1]["type"] in {"verified", "verified_with_warnings"}
            commits = [row["result"]["commit_step_outcome"] for row in events if row["node"] == "commit_step"]
            assert len(commits) == 1 and commits[0]["type"] == "next_step_ready"
            assert durable["task_state"]["typed_recovery_receipts"]["wgs-repair-observation-and-reader"]
            assert durable["task_state"]["typed_recovery_budget"] == {recovery_model.incident: 1}
            assert durable["steps"][0]["status"] in {"done", "passed", "completed"}
            assert not durable["task_state"].get("managed_run_stop")
            assert len(next_requests_seen) == 1
            _save(evidence, "acceptance", {"status": "passed", "case": first_claim, "approvals": approvals,
                "dispatches": dispatches, "verifications": verifies, "commits": commits,
                "source": bio_agents.__file__, "source_action_sha256": _ORIGINAL_ACTION_SHA256})
        finally:
            _save(evidence, "provider-requests", model.calls)
            _save(evidence, "approvals", approvals)
            if recovery_model:
                _save(evidence, "recovery-requests", recovery_model.requests)
            await h.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=90))
