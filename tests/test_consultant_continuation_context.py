from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent_core.consultant_continuation import (
    CONSULTANT_PREPROCESS_PHASES,
    ConsultantPreprocessRecorder,
    build_continuation_cache,
    build_continuation_identity,
    consultant_discovery_delta,
    evaluate_continuation_cache,
    run_optional_enrichment,
)
from agent_core.consultation_state import (
    build_consultant_candidate,
    evaluate_consultant_candidate_route,
    new_consultation_state,
    seed_structured_evidence,
)
from agent_core.context_lineage import ContextLineageStore
from agent_core.skill_discovery import (
    CAPABILITY_GAP,
    SELECTED,
    capability_gap_allowed,
    discovery_next_actions,
    new_skill_discovery_state,
    record_catalog_expansion,
    record_capability_gap,
    record_loaded_skill_doc,
    record_reasoning_escalation,
    record_rejected_candidate,
    record_selected_skills,
    record_skill_search,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import TASK_SPEC_MUTATION_SCHEMA_VERSION
from prompts.manager import PromptManager


ROOT = Path(__file__).resolve().parents[1]
SKILL_ID = "genomics/snpeff-annotation"
PROFILING_PATH = ROOT / "execution" / "profiling.py"
_profiling_spec = importlib.util.spec_from_file_location(
    "consultant_continuation_profiling_under_test", PROFILING_PATH
)
_profiling_module = importlib.util.module_from_spec(_profiling_spec)
assert _profiling_spec and _profiling_spec.loader
_profiling_spec.loader.exec_module(_profiling_module)
RunProfiler = _profiling_module.RunProfiler


def mutation(*operations: dict) -> dict:
    return {
        "proposal_type": "task_spec_mutation",
        "schema_version": TASK_SPEC_MUTATION_SCHEMA_VERSION,
        "operations": list(operations),
        "unresolved_ambiguities": [],
        "proposal_evidence_refs": [],
    }


def op(name: str, **fields: object) -> dict:
    return {
        "op": name,
        **fields,
        "reason": "consultant_interpretation",
        "supporting_evidence": ["U1"],
    }


def identity(
    *,
    head_ref: str = "turn-U1",
    task_spec: dict | None = None,
    bindings: dict | None = None,
    work_dir: str = ".",
) -> dict:
    return build_continuation_identity(
        task_id="task-continuation",
        consultation_id="consultation-1",
        conversation_evidence={
            "head_ref": head_ref,
            "turn_count": 1,
            "user_turn_count": 1,
        },
        task_spec=task_spec or {"goal": "Annotate variants"},
        project_id="default",
        task_mode="managed",
        task_ownership={"request_kind": "execute", "scope": "managed"},
        resolved_bindings=bindings or {},
        work_dir=work_dir,
    )


class ConsultantContinuationContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skills = SkillManager(str(ROOT / "skills"))

    def _ready_candidate(self, *, runtime_ready: bool = True) -> dict:
        consultation = new_consultation_state("consultation-route", "task-route")
        consultation = seed_structured_evidence(
            consultation,
            candidate_skill_ids=[SKILL_ID],
            routing_candidate_ids=[SKILL_ID],
            runtime_evidence={
                SKILL_ID: {
                    "status": "ready" if runtime_ready else "unavailable",
                    "healthcheck_status": "passed" if runtime_ready else "failed",
                }
            },
        )
        return build_consultant_candidate(
            None,
            consultation,
            mutation(
                op("set_goal", value="Annotate supplied variants"),
                op("upsert_input", input={"role": "variant_vcf", "path": "/data/A.vcf"}),
            ),
            selected_skills=[SKILL_ID],
            task_id="task-route",
            renderer_records=[self.skills.get_renderer_record(SKILL_ID)],
            input_audit={
                "relevant": True,
                "status": "resolved",
                "resolved": ["/data/A.vcf"],
            },
            runtime_evidence={
                SKILL_ID: {
                    "status": "ready" if runtime_ready else "unavailable",
                    "healthcheck_status": "passed" if runtime_ready else "failed",
                }
            },
        )

    def test_p1_same_episode_reuses_stable_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "variants.vcf"
            input_path.write_text("##fileformat=VCFv4.2\n", encoding="utf-8")
            current_identity = identity(work_dir=tmpdir)
            base = {
                "consultant_context": "annotate variants",
                "consultant_memory_retrieval_context": "bounded memory",
                "input_resolution_result": {
                    "resolved_inputs": [{"path": str(input_path)}],
                },
            }
            cache = build_continuation_cache(identity=current_identity, base=base)
            decision = evaluate_continuation_cache(cache, identity=current_identity)

        self.assertTrue(decision["cache_hit"])
        self.assertEqual(decision["base"]["consultant_context"], "annotate variants")
        recorder = ConsultantPreprocessRecorder(
            task_id="task-continuation", episode_id=current_identity["episode_id"], invocation=2
        )
        recorder.mark("memory", cache_hit=True, call_count=0)
        memory_row = next(row for row in recorder.phase_rows() if row["phase"] == "memory")
        self.assertTrue(memory_row["cache_hit"])
        self.assertEqual(memory_row["call_count"], 0)

    def test_p2_new_user_turn_invalidates_episode(self) -> None:
        first = identity(head_ref="turn-U1")
        cache = build_continuation_cache(identity=first, base={})
        second = identity(head_ref="turn-U2")
        decision = evaluate_continuation_cache(cache, identity=second)
        self.assertFalse(decision["cache_hit"])
        self.assertIn(decision["reason"], {"episode_changed", "conversation_changed"})

    def test_p3_material_taskspec_or_binding_change_invalidates(self) -> None:
        first = identity(task_spec={"goal": "Annotate variants"})
        cache = build_continuation_cache(identity=first, base={})
        changed_spec = identity(task_spec={"goal": "Filter variants"})
        self.assertEqual(
            evaluate_continuation_cache(cache, identity=changed_spec)["reason"],
            "task_spec_material_change",
        )
        changed_binding = identity(
            task_spec={"goal": "Annotate variants"},
            bindings={"variant_vcf": "/data/B.vcf"},
        )
        self.assertEqual(
            evaluate_continuation_cache(cache, identity=changed_binding)["reason"],
            "formal_binding_changed",
        )

    def test_p4_delta_keeps_new_discovery_facts_and_budgets(self) -> None:
        state = record_skill_search(
            new_skill_discovery_state(),
            query="variant effect annotation",
            candidate_cards=[
                {"skill_id": SKILL_ID, "score": 10, "reasons": ["match"]},
                {"skill_id": "genomics/incompatible", "score": 3, "reasons": ["weak"]},
            ],
        )
        state = record_loaded_skill_doc(state, skill_id=SKILL_ID, document="compatible")
        state = record_rejected_candidate(
            state,
            skill_id="genomics/incompatible",
            reason="documented input contract is incompatible",
        )
        state = record_selected_skills(state, [SKILL_ID])
        state["reasoning_level"] = "escalated"
        state["reasoning_escalation_count"] = 1
        delta = consultant_discovery_delta(state)
        self.assertEqual(delta["routing_status"], SELECTED)
        self.assertIn(SKILL_ID, delta["loaded_skill_docs"])
        self.assertEqual(delta["rejected_candidates"][0]["skill_id"], "genomics/incompatible")
        self.assertEqual(delta["reasoning_level"], "escalated")
        self.assertEqual(delta["remaining_discovery_budget"]["loaded_skill_docs"], 4)
        self.assertEqual(discovery_next_actions(state), ["COMPLETE_TASK_SEMANTICS"])

    def test_p5_complete_select_candidate_routes_directly_to_planner(self) -> None:
        candidate = self._ready_candidate(runtime_ready=True)
        decision = evaluate_consultant_candidate_route(candidate)
        self.assertEqual(decision["route"], "planner")
        self.assertEqual(decision["code"], "CONSULTANT_CANDIDATE_COMPLETE")

    def test_p6_legacy_candidate_gate_is_not_schema2_runtime_authority(self) -> None:
        candidate = self._ready_candidate(runtime_ready=False)
        decision = evaluate_consultant_candidate_route(candidate)
        # Retained only for legacy FINISH compatibility. Schema-2 uses the
        # explicit handoff protocol and leaves runtime facts to Agent reasoning.
        self.assertEqual(decision["route"], "planner")

    def test_p7_select_and_finish_use_the_same_route_gate(self) -> None:
        candidate = self._ready_candidate(runtime_ready=True)
        select_decision = evaluate_consultant_candidate_route(candidate)
        finish_decision = evaluate_consultant_candidate_route(candidate)
        self.assertEqual(select_decision, finish_decision)
        self.assertEqual(
            evaluate_consultant_candidate_route({})["code"],
            "CANONICAL_TASKSPEC_REQUIRED",
        )
        pending = json.loads(json.dumps(candidate))
        pending["pending_question"] = {
            "owner": "user",
            "question": "Which user-owned scientific choice?",
        }
        self.assertEqual(
            evaluate_consultant_candidate_route(pending)["code"],
            "PENDING_USER_QUESTION",
        )

    def test_p8_timing_rows_cover_every_phase_and_batch_trace_is_redacted(self) -> None:
        recorder = ConsultantPreprocessRecorder(
            task_id="task-trace", episode_id="episode-1", invocation=1
        )
        for phase in CONSULTANT_PREPROCESS_PHASES:
            recorder.mark(
                phase,
                elapsed_ms=1.0,
                call_count=1,
                secret="sensitive-value-not-written",
            )
        rows = recorder.event_rows()
        self.assertEqual(
            [row["payload"]["phase"] for row in rows],
            list(CONSULTANT_PREPROCESS_PHASES),
        )
        self.assertNotIn("sensitive-value-not-written", json.dumps(rows))
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-trace")
            stored = store.record_events(rows)
            event_lines = (store.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(stored), len(CONSULTANT_PREPROCESS_PHASES))
        self.assertEqual(len(event_lines), len(CONSULTANT_PREPROCESS_PHASES))

    def test_p9_progress_is_observation_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profiler = RunProfiler(Path(tmpdir))
            profiler.start_task("task-progress")
            for code in (
                "CONSULTANT_SKILL_DOC_LOADED",
                "CONSULTANT_SKILL_SELECTED",
                "CONSULTANT_REASONING_ESCALATED",
            ):
                profiler.record_consultant_progress(
                    "task-progress",
                    code=code,
                    message="Observation-only discovery progress.",
                    detail={"skill_id": SKILL_ID},
                )
            events = [
                row for row in profiler.snapshot("task-progress")["events"]
                if row.get("type") == "consultant_progress"
            ]
        self.assertEqual(
            {event["code"] for event in events},
            {
                "CONSULTANT_SKILL_DOC_LOADED",
                "CONSULTANT_SKILL_SELECTED",
                "CONSULTANT_REASONING_ESCALATED",
            },
        )
        self.assertTrue(all(event["semantic_effect"] == "none" for event in events))
        self.assertTrue(all("task_spec" not in event for event in events))

    def test_p10_first_empty_search_cannot_terminalize_capability_gap(self) -> None:
        state = record_skill_search(
            new_skill_discovery_state(), query="first weak query", candidate_cards=[]
        )
        self.assertFalse(capability_gap_allowed(state))
        self.assertEqual(discovery_next_actions(state), ["SEARCH_SKILLS"])
        with self.assertRaises(ValueError):
            record_capability_gap(state, reason="premature")

    def test_p11_escalated_search_can_recover_load_select_and_complete(self) -> None:
        state = record_skill_search(
            new_skill_discovery_state(), query="query one", candidate_cards=[]
        )
        state = record_skill_search(state, query="query two", candidate_cards=[])
        state = record_catalog_expansion(state, category="genomics", candidate_cards=[])
        state = record_reasoning_escalation(state, trigger="normal discovery exhausted")
        state = record_skill_search(
            state,
            query="existing VCF functional annotation",
            candidate_cards=[{"skill_id": SKILL_ID, "score": 20, "reasons": ["documented match"]}],
            source="reasoning_escalation_search",
        )
        state = record_loaded_skill_doc(
            state, skill_id=SKILL_ID, document=self.skills.load_skill(SKILL_ID)
        )
        state = record_selected_skills(state, [SKILL_ID])
        self.assertEqual(state["routing_status"], SELECTED)
        self.assertEqual(evaluate_consultant_candidate_route(self._ready_candidate())["route"], "planner")

    def test_p12_large_catalog_listing_is_bounded_metadata_only(self) -> None:
        cards = self.skills.list_skill_cards("all", max_results=5)
        self.assertEqual(len(cards), 5)
        for card in cards:
            self.assertNotIn("document", card)
            self.assertNotIn("skill_md", card)
            self.assertLess(len(json.dumps(card, ensure_ascii=False)), 4000)

    def test_p13_optional_archive_timeout_fails_open(self) -> None:
        result = asyncio.run(run_optional_enrichment(
            lambda: time.sleep(0.2),
            timeout_seconds=0.01,
        ))
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["failed_open"])
        self.assertEqual(result["value"], "")

    def test_q1_consultant_reasoning_policy_is_independent_from_recovery(self) -> None:
        env = os.environ.copy()
        env.update({
            "BIOMIND_RECOVERY_THINKING_ENABLED": "0",
            "BIOMIND_CONSULTANT_DISCOVERY_THINKING_ENABLED": "1",
            "BIOMIND_RECOVERY_REASONING_EFFORT": "max",
            "BIOMIND_CONSULTANT_DISCOVERY_REASONING_EFFORT": "high",
        })
        output = subprocess.check_output(
            [
                sys.executable,
                "-c",
                (
                    "from config.config import "
                    "RECOVERY_THINKING_ENABLED,CONSULTANT_DISCOVERY_THINKING_ENABLED,"
                    "RECOVERY_REASONING_EFFORT,CONSULTANT_DISCOVERY_REASONING_EFFORT;"
                    "print(RECOVERY_THINKING_ENABLED,CONSULTANT_DISCOVERY_THINKING_ENABLED,"
                    "RECOVERY_REASONING_EFFORT,CONSULTANT_DISCOVERY_REASONING_EFFORT)"
                ),
            ],
            cwd=ROOT,
            env=env,
            text=True,
        ).strip()
        self.assertEqual(output, "False True max high")

    def test_select_prompt_requires_explicit_answer_or_planning_handoff(self) -> None:
        prompt = PromptManager.get_consultant_v2_prompt({})
        select_example = next(
            line for line in prompt.splitlines()
            if '"action_type":"select_skill"' in line
        )
        self.assertIn("task_spec_operations", select_example)
        self.assertIn("Selection alone does not enter Planner", prompt)
        self.assertIn("submit an explicit planning handoff", prompt)
        self.assertIn('"action_type":"answer_user"', prompt)


if __name__ == "__main__":
    unittest.main()
