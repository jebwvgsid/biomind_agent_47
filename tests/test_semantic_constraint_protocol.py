import json
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent_core.knowledge_conflicts import (
    ClaimRegistry,
    KnowledgeConflictDetector,
    semantic_claim_set_hash,
)
from agent_core.plan_tracker import (
    approval_semantic_delta,
    approve_plan_state,
    bind_renderer_invocation_contracts,
    build_plan_state,
    parse_plan_steps,
)
from agent_core.task_spec import (
    canonical_constraint_hash,
    create_task_spec,
    evaluate_failure_policies,
    merge_compiled_constraints,
    merge_task_spec,
    project_task_spec_to_renderer,
    semantic_constraints_from_task_spec,
    semantic_install_policy,
    semantic_provenance_catalog,
    validate_compiled_constraints,
)
from bio_agents import BioAnalystAgent
from prompts.manager import PromptManager


FIXTURES = Path(__file__).parent / "fixtures"


def compiled_row(
    key,
    value,
    *,
    operator="eq",
    strength="hard",
    confidence=0.95,
    source_text="mocked Consultant extraction",
    **extra,
):
    return {
        "constraint_id": "mock-" + key.replace(".", "-"),
        "semantic_key": key,
        "operator": operator,
        "value": value,
        "strength": strength,
        "confidence": confidence,
        "source_text": source_text,
        **extra,
    }


def register_typed(registry, source_type, key, value, *, operator="eq", strength="required"):
    return registry.register(
        f"{key} {operator} {value}",
        source_type=source_type,
        strength=strength,
        scope="task" if source_type == "task_contract" else "stage",
        scope_id="task" if source_type == "task_contract" else "step-1",
        attributes={
            "semantic_key": key,
            "fact_key": key,
            "operator": operator,
            "value": value,
        },
    )


class SemanticConstraintProtocolTests(unittest.TestCase):
    def test_mocked_consultant_language_variants_share_schema_invariants(self):
        variants = [
            "必须使用 genome.gtf",
            "gtf 就用我给的那个",
            "注释文件别换",
            "那个 genome.gtf 就行",
            "gtf必需用我给de，别换",
        ]
        for utterance in variants:
            with self.subTest(utterance=utterance):
                mocked_output = [compiled_row(
                    "required_input.annotation_gtf",
                    "/formal/genome.gtf",
                    confidence=0.82 if "de" in utterance else 0.98,
                    source_text=utterance,
                )]
                validated = validate_compiled_constraints(mocked_output)
                self.assertTrue(validated["valid"], validated["errors"])
                item = validated["constraints"][0]
                self.assertEqual(item["semantic_key"], "required_input.annotation_gtf")
                self.assertEqual(item["value"], "/formal/genome.gtf")
                self.assertEqual(item["strength"], "hard")
                self.assertIsInstance(item["confidence"], float)
                self.assertTrue(item["source_text"])

    def test_conditional_preference_and_ambiguity_remain_distinct(self):
        rows = [
            compiled_row(
                "failure_policy.java_missing",
                "block",
                operator="on_condition",
                strength="conditional",
                condition="java_missing",
                source_text="如果 Java 没有，就 blocked",
            ),
            compiled_row(
                "preference.database_source",
                "custom_genome",
                operator="prefer",
                strength="preference",
                confidence=0.75,
                source_text="最好使用 custom genome",
            ),
            compiled_row(
                "required_behavior.reference_choice",
                "unmapped",
                strength="ambiguous",
                confidence=0.3,
                ambiguity="UNMAPPED",
                needs_clarification=True,
                source_text="还是那个吧",
            ),
        ]
        result = validate_compiled_constraints(rows)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["metrics"]["conditional_count"], 1)
        self.assertEqual(result["metrics"]["preference_count"], 1)
        self.assertEqual(result["metrics"]["ambiguous_count"], 1)
        self.assertEqual(result["constraints"][0]["operator"], "on_condition")

    def test_invalid_consultant_payload_requires_structured_retry(self):
        result = validate_compiled_constraints("the complete raw prompt is a hard constraint")
        self.assertFalse(result["valid"])
        self.assertEqual(result["metrics"]["schema_validation"], "failed")
        self.assertEqual(result["constraints"], [])

    def test_consultant_json_payload_preserves_semantic_ir(self):
        payload = {
            "action_type": "finish",
            "selected_skills": ["genomics/snpeff-annotation"],
            "summary": "ready",
            "semantic_constraints": [compiled_row("required_input.annotation_gtf", "/formal/genome.gtf")],
        }
        wire = "<action_json>" + json.dumps(payload) + "</action_json>"
        self.assertEqual(BioAnalystAgent._consultant_json_payload(wire), payload)
        self.assertTrue(BioAnalystAgent._consultant_command_from_json(wire).startswith("FINISH"))

    def test_contract_repair_preserves_complete_payload_and_only_allows_failed_fields(self):
        source_id = "msg-contract-repair"
        payload = {
            "action_type": "finish",
            "selected_skills": ["genomics/snpeff-annotation"],
            "summary": "[SELECTED_SKILLS: genomics/snpeff-annotation] ready",
            "semantic_constraints": [
                compiled_row("required_behavior.annotation_format", "gtf22", source_text=""),
                compiled_row(
                    "forbidden_behavior.species_database_guess",
                    True,
                    operator="forbid",
                    source_text="",
                ),
            ],
        }
        validation = validate_compiled_constraints(
            payload["semantic_constraints"],
            provenance_catalog=[{"source_id": source_id, "source_type": "user_explicit"}],
            require_resolvable_provenance=True,
        )
        self.assertFalse(validation["valid"])
        repair = BioAnalystAgent._consultant_contract_repair_state(
            payload,
            validation["errors"],
        )
        self.assertEqual(repair["original_payload"], payload)
        self.assertEqual(repair["base_payload"], payload)
        self.assertEqual(repair["error_count"], 2)
        context = BioAnalystAgent._consultant_contract_repair_context(repair)
        self.assertIn("required_behavior.annotation_format", context)
        self.assertIn('"error_count": 2', context)

        illegal = BioAnalystAgent._apply_consultant_contract_repair(repair, {
            "action_type": "repair_finish",
            "base_payload_hash": repair["payload_hash"],
            "constraint_patches": [{
                "index": 0,
                "set": {
                    "semantic_key": "param.gtf22",
                    "provenance": [{"source_type": "user_explicit", "source_id": source_id}],
                },
            }],
        })
        self.assertFalse(illegal["valid"])
        self.assertEqual(illegal["payload"], payload)

        repaired = BioAnalystAgent._apply_consultant_contract_repair(repair, {
            "action_type": "repair_finish",
            "base_payload_hash": repair["payload_hash"],
            "constraint_patches": [
                {
                    "index": index,
                    "set": {"provenance": [{"source_type": "user_explicit", "source_id": source_id}]},
                }
                for index in range(2)
            ],
        })
        self.assertTrue(repaired["valid"], repaired["errors"])
        self.assertEqual(
            [row["semantic_key"] for row in repaired["payload"]["semantic_constraints"]],
            [row["semantic_key"] for row in payload["semantic_constraints"]],
        )
        self.assertTrue(validate_compiled_constraints(
            repaired["payload"]["semantic_constraints"],
            provenance_catalog=[{"source_id": source_id, "source_type": "user_explicit"}],
            require_resolvable_provenance=True,
        )["valid"])

    def test_multiround_repair_keeps_first_payload_immutable(self):
        source_id = "msg-multiround"
        payload = {
            "action_type": "finish",
            "selected_skills": ["genomics/snpeff-annotation"],
            "summary": "[SELECTED_SKILLS: genomics/snpeff-annotation] ready",
            "semantic_constraints": [
                compiled_row("required_output.result_a", "a.tsv", source_text=""),
                compiled_row("required_output.result_b", "b.tsv", source_text=""),
            ],
        }
        first_validation = validate_compiled_constraints(
            payload["semantic_constraints"],
            provenance_catalog=[{"source_id": source_id, "source_type": "user_explicit"}],
            require_resolvable_provenance=True,
        )
        first_state = BioAnalystAgent._consultant_contract_repair_state(
            payload,
            first_validation["errors"],
        )
        partially_repaired = BioAnalystAgent._apply_consultant_contract_repair(first_state, {
            "action_type": "repair_finish",
            "base_payload_hash": first_state["payload_hash"],
            "constraint_patches": [{
                "index": 0,
                "set": {"provenance": [{"source_type": "user_explicit", "source_id": source_id}]},
            }],
        })
        self.assertTrue(partially_repaired["valid"], partially_repaired["errors"])
        second_validation = validate_compiled_constraints(
            partially_repaired["payload"]["semantic_constraints"],
            provenance_catalog=[{"source_id": source_id, "source_type": "user_explicit"}],
            require_resolvable_provenance=True,
        )
        self.assertFalse(second_validation["valid"])
        second_state = BioAnalystAgent._consultant_contract_repair_state(
            partially_repaired["payload"],
            second_validation["errors"],
            previous=first_state,
        )

        self.assertEqual(second_state["original_payload"], payload)
        self.assertEqual(second_state["base_payload"], partially_repaired["payload"])
        self.assertEqual(second_state["initial_payload_hash"], first_state["initial_payload_hash"])
        self.assertNotEqual(second_state["payload_hash"], second_state["initial_payload_hash"])
        context = BioAnalystAgent._consultant_contract_repair_context(second_state)
        self.assertIn('"original_payload"', context)
        self.assertIn('"base_payload"', context)

    def test_raw_finish_payload_remains_visible_in_repair_history(self):
        payload = {
            "action_type": "finish",
            "selected_skills": ["genomics/snpeff-annotation"],
            "summary": "[SELECTED_SKILLS: genomics/snpeff-annotation] ready",
            "semantic_constraints": [
                compiled_row(f"required_output.result_{index}", f"result_{index}.tsv")
                for index in range(16)
            ],
        }
        raw = "<action_json>" + json.dumps(payload, ensure_ascii=False) + "</action_json>"
        error = (
            "<consultant_semantic_constraint_validation_error>"
            + json.dumps([{"index": index, "field": "provenance"} for index in range(16)])
            + "</consultant_semantic_constraint_validation_error>"
        )
        visible = BioAnalystAgent._consultant_visible_messages([
            HumanMessage(content="authoritative request"),
            AIMessage(content=raw),
            SystemMessage(content=error),
        ])
        raw_message = next(message for message in visible if isinstance(message, AIMessage))
        self.assertEqual(raw_message.content, raw)
        self.assertEqual(len(BioAnalystAgent._consultant_json_payload(raw_message.content)["semantic_constraints"]), 16)

    def test_semantic_aliases_are_canonicalized_with_audit(self):
        source_id = "msg-alias-source"
        provenance = [{"source_type": "user_explicit", "source_id": source_id}]
        rows = [
            compiled_row("forbidden.public_db_guess", "do not guess", operator="forbid", source_text="", provenance=provenance),
            compiled_row("forbidden.demo_data", "do not use demo", operator="forbid", source_text="", provenance=provenance),
            compiled_row("runtime.snpeff_jar", "/env/snpEff.jar", source_text="", provenance=provenance),
            compiled_row("runtime.env", "bio_gatk", source_text="", provenance=provenance),
            compiled_row("param.gtf22", True, source_text="", provenance=provenance),
        ]
        validation = validate_compiled_constraints(
            rows,
            provenance_catalog=[{"source_id": source_id, "source_type": "user_explicit"}],
            require_resolvable_provenance=True,
        )
        self.assertTrue(validation["valid"], validation["errors"])
        by_key = {row["semantic_key"]: row for row in validation["constraints"]}
        self.assertEqual(by_key["required_behavior.annotation_format"]["value"], "gtf22")
        self.assertIs(by_key["forbidden_behavior.species_database_guess"]["value"], True)
        self.assertIs(by_key["forbidden_behavior.demo_or_toy_input"]["value"], True)
        self.assertIn("runtime_constraint.snpeff_jar", by_key)
        self.assertIn("runtime_constraint.environment", by_key)
        self.assertEqual(validation["metrics"]["normalization_count"], 5)
        self.assertEqual(
            by_key["required_behavior.annotation_format"]["normalization_audit"]["rule_id"],
            "ontology-alias-v1",
        )

    def test_strict_provenance_rejects_empty_and_unknown_source_ids(self):
        empty = compiled_row(
            "required_behavior.annotation_format",
            "gtf22",
            source_text="",
            provenance=[{"source_type": "user_explicit", "source_ref": ""}],
        )
        validation = validate_compiled_constraints(
            [empty],
            provenance_catalog=[{"source_id": "msg-known", "source_type": "user_explicit"}],
            require_resolvable_provenance=True,
        )
        self.assertFalse(validation["valid"])
        self.assertIn("non-empty source_id", validation["errors"][0]["reason"])

        unknown = {**empty, "provenance": [{"source_type": "user_explicit", "source_id": "msg-invented"}]}
        validation = validate_compiled_constraints(
            [unknown],
            provenance_catalog=[{"source_id": "msg-known", "source_type": "user_explicit"}],
            require_resolvable_provenance=True,
        )
        self.assertFalse(validation["valid"])
        self.assertIn("unknown source_id", validation["errors"][0]["reason"])

    def test_supplied_provenance_catalog_rejects_forged_source_type(self):
        forged = compiled_row(
            "required_behavior.annotation_format",
            "gtf22",
            source_text="",
            provenance=[{
                "source_type": "forged_superuser",
                "source_id": "msg-known",
            }],
        )
        validation = validate_compiled_constraints(
            [forged],
            provenance_catalog=[{
                "source_id": "msg-known",
                "source_type": "user_explicit",
            }],
            require_resolvable_provenance=True,
        )
        self.assertFalse(validation["valid"])
        self.assertEqual({error["field"] for error in validation["errors"]}, {"provenance"})
        self.assertIn("expected user_explicit, got forged_superuser", validation["errors"][0]["reason"])

        catalog_is_the_type_boundary = validate_compiled_constraints(
            [forged],
            provenance_catalog={"msg-known": "user_explicit"},
        )
        self.assertFalse(catalog_is_the_type_boundary["valid"])
        self.assertIn("source_type mismatch", catalog_is_the_type_boundary["errors"][0]["reason"])

        # Legacy controller-owned callers that do not supply a catalog retain
        # their prior schema-only behavior.
        without_catalog = validate_compiled_constraints(
            [forged],
            require_resolvable_provenance=True,
        )
        self.assertTrue(without_catalog["valid"], without_catalog["errors"])

    def test_conflicting_provenance_catalog_types_are_rejected(self):
        row = compiled_row(
            "required_behavior.annotation_format",
            "gtf22",
            source_text="",
            provenance=[{
                "source_type": "user_explicit",
                "source_id": "msg-known",
            }],
        )
        validation = validate_compiled_constraints(
            [row],
            provenance_catalog=[
                {"source_id": "msg-known", "source_type": "user_explicit"},
                {"source_id": "msg-known", "source_type": "forged_superuser"},
            ],
            require_resolvable_provenance=True,
        )
        self.assertFalse(validation["valid"])
        self.assertEqual(validation["errors"][0]["field"], "provenance")
        self.assertIn("conflicting provenance catalog source_type", validation["errors"][0]["reason"])

    def test_top_level_semantic_array_repair_is_hash_bound(self):
        validation = validate_compiled_constraints(
            None,
            provenance_catalog=[{"source_id": "msg-known", "source_type": "user_explicit"}],
            require_resolvable_provenance=True,
        )
        repair = BioAnalystAgent._consultant_contract_repair_state(
            {"action_type": "finish", "selected_skills": [], "summary": "ready"},
            validation["errors"],
        )
        row = compiled_row(
            "required_behavior.annotation_format",
            "gtf22",
            source_text="",
            provenance=[{"source_type": "user_explicit", "source_id": "msg-known"}],
        )
        applied = BioAnalystAgent._apply_consultant_contract_repair(repair, {
            "action_type": "repair_finish",
            "base_payload_hash": repair["payload_hash"],
            "constraint_patches": [],
            "set": {"semantic_constraints": [row]},
        })
        self.assertTrue(applied["valid"], applied["errors"])
        self.assertEqual(applied["payload"]["semantic_constraints"], [row])
        wrong_hash = BioAnalystAgent._apply_consultant_contract_repair(repair, {
            "action_type": "repair_finish",
            "base_payload_hash": "wrong",
            "constraint_patches": [],
            "set": {"semantic_constraints": [row]},
        })
        self.assertFalse(wrong_hash["valid"])

    def test_exact_base_constraint_inherits_resolvable_provenance(self):
        spec = create_task_spec(
            "Use /formal/genome.gtf and -gtf22.",
            task_id="base-inheritance",
        )
        base = semantic_constraints_from_task_spec(spec)
        catalog = semantic_provenance_catalog(spec)
        emitted = [
            {
                "constraint_id": row["requirement_id"],
                "semantic_key": row["semantic_key"],
                "operator": row["operator"],
                "value": row["value"],
                "strength": row["strength"],
                "confidence": row["confidence"],
            }
            for row in base
        ]
        validation = validate_compiled_constraints(
            emitted,
            provenance_catalog=catalog,
            require_resolvable_provenance=True,
            base_constraints=base,
        )
        self.assertTrue(validation["valid"], validation["errors"])
        self.assertEqual(validation["metrics"]["provenance_inheritance_count"], len(emitted))
        self.assertIn(
            "required_behavior.annotation_format",
            {row["semantic_key"] for row in validation["constraints"]},
        )

    def test_schema_failures_never_route_to_user_but_real_ambiguity_does(self):
        self.assertEqual(
            BioAnalystAgent._consultant_semantic_failure_route(
                repair_attempt=2,
                ambiguity_retry_count=0,
            ),
            "consultant",
        )
        self.assertEqual(
            BioAnalystAgent._consultant_semantic_failure_route(
                repair_attempt=3,
                ambiguity_retry_count=0,
            ),
            "blocked_internal_contract_error",
        )
        self.assertEqual(
            BioAnalystAgent._consultant_semantic_failure_route(
                repair_attempt=0,
                ambiguity_retry_count=2,
            ),
            "ask_user",
        )

    def test_finalize_prompt_uses_delta_schema_source_catalog_and_patch_only_repair(self):
        prompt = PromptManager.get_consultant_finalize_system_prompt(
            finalize_context_id="context-1",
            base_contract_hash="contract-1",
            allowed_actions=["finish"],
        )
        self.assertIn('"source_id"', prompt)
        self.assertIn("semantic delta needed for `coverage_gaps`", prompt)
        self.assertNotIn("repair_finish", prompt)

        repair_prompt = PromptManager.get_consultant_finalize_field_repair_prompt(
            finalize_context_id="context-1",
            base_contract_hash="contract-1",
            base_payload_hash="payload-1",
            allowed_fields_by_index={0: ["provenance"]},
        )
        self.assertIn("Do not regenerate", repair_prompt)
        self.assertIn('"const": "repair_finish"', repair_prompt)

    def test_session_1786203096_real_responses_converge_without_schema_drift_or_reexecute(self):
        trace_root = (
            Path(__file__).resolve().parents[2]
            / "benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1786203096"
        )
        first_path = trace_root / "context_trace/consultant-0003-4f58e4794999/response.json"
        second_path = trace_root / "context_trace/consultant-0004-bd8dabf113b9/response.json"
        metrics_path = trace_root / "run_metrics.json"
        if not all(path.is_file() for path in (first_path, second_path, metrics_path)):
            self.skipTest("session_1786203096 trace is unavailable")

        first_content = json.loads(first_path.read_text(encoding="utf-8"))["content"]
        second_content = json.loads(second_path.read_text(encoding="utf-8"))["content"]
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        first = BioAnalystAgent._consultant_json_payload(first_content)
        second = BioAnalystAgent._consultant_json_payload(second_content)
        self.assertEqual(len(first["semantic_constraints"]), 16)
        self.assertGreater(len(second["semantic_constraints"]), len(first["semantic_constraints"]))
        self.assertEqual(metrics["summary"]["commands"], 1)

        task_id = metrics["task_id"]
        spec = create_task_spec(metrics["metadata"]["test_prompt"], task_id=task_id)
        evidence_id = "evidence:2bbe0195b77ee618"
        consultation_state = {
            "phase": "finalize",
            "execute_call_count": 1,
            "unresolved_requirement_ids": [],
            "evidence": [{"evidence_id": evidence_id, "status": "succeeded"}],
            "requirements": {},
        }
        base = semantic_constraints_from_task_spec(
            spec,
            selected_skill_ids=first["selected_skills"],
        )
        catalog = semantic_provenance_catalog(
            spec,
            consultation_state=consultation_state,
            selected_skill_ids=first["selected_skills"],
        )
        validation = validate_compiled_constraints(
            first["semantic_constraints"],
            provenance_catalog=catalog,
            require_resolvable_provenance=True,
            base_constraints=base,
        )
        self.assertFalse(validation["valid"])
        self.assertEqual({error["field"] for error in validation["errors"]}, {"provenance"})
        repair = BioAnalystAgent._consultant_contract_repair_state(first, validation["errors"])
        self.assertEqual(repair["original_payload"], first)

        # The historical second response regenerated the whole payload.  The
        # fixed controller rejects it and therefore cannot accept its changed
        # semantic keys or trigger another environment probe.
        rejected_regeneration = BioAnalystAgent._apply_consultant_contract_repair(repair, second)
        self.assertFalse(rejected_regeneration["valid"])
        self.assertEqual(rejected_regeneration["payload"], first)
        self.assertEqual(consultation_state["execute_call_count"], 1)

        user_source_id = spec["source_refs"][0]
        patches_by_index = {}
        for error in validation["errors"]:
            index = int(error["index"])
            semantic_key = first["semantic_constraints"][index]["semantic_key"]
            source_type = "runtime_evidence" if semantic_key.startswith("runtime_constraint.") else "user_explicit"
            source_id = evidence_id if source_type == "runtime_evidence" else user_source_id
            patches_by_index.setdefault(index, {})[error["field"]] = [{
                "source_type": source_type,
                "source_id": source_id,
            }]
        repaired = BioAnalystAgent._apply_consultant_contract_repair(repair, {
            "action_type": "repair_finish",
            "base_payload_hash": repair["payload_hash"],
            "constraint_patches": [
                {"index": index, "set": changes}
                for index, changes in sorted(patches_by_index.items())
            ],
        })
        self.assertTrue(repaired["valid"], repaired["errors"])
        repaired_validation = validate_compiled_constraints(
            repaired["payload"]["semantic_constraints"],
            provenance_catalog=catalog,
            require_resolvable_provenance=True,
            base_constraints=base,
        )
        self.assertTrue(repaired_validation["valid"], repaired_validation["errors"])
        self.assertFalse(any(
            row.get("needs_clarification")
            for row in repaired_validation["constraints"]
        ))

        controller_base = semantic_constraints_from_task_spec(spec)
        delta = [
            row for row in repaired_validation["constraints"]
            if not row["semantic_key"].startswith("required_skill.")
        ]
        compiled = merge_compiled_constraints(spec, [*controller_base, *delta])
        selected_base = semantic_constraints_from_task_spec(
            compiled,
            selected_skill_ids=first["selected_skills"],
        )
        compiled = merge_compiled_constraints(compiled, selected_base)
        final_constraints = semantic_constraints_from_task_spec(
            compiled,
            selected_skill_ids=first["selected_skills"],
        )
        final_keys = {row["semantic_key"] for row in final_constraints}
        self.assertIn("required_behavior.annotation_format", final_keys)
        self.assertIn("forbidden_behavior.species_database_guess", final_keys)
        self.assertIn("forbidden_behavior.demo_or_toy_input", final_keys)
        self.assertIn("failure_policy.snpeff_missing", final_keys)
        self.assertIn("runtime_constraint.contig_check", final_keys)
        self.assertIn("required_skill.genomics_snpeff_annotation", final_keys)
        self.assertTrue(all(
            any(
                str(source.get("source_id") or source.get("source_ref") or "").strip()
                for source in row.get("provenance", [])
                if isinstance(source, dict)
            )
            for row in final_constraints
        ))
        self.assertEqual(consultation_state["unresolved_requirement_ids"], [])

    def test_task_spec_consumes_compiled_contract_without_promoting_raw_prompt(self):
        raw = "If Java is missing block; use /formal/genome.gtf and do not guess a database."
        spec = create_task_spec(raw)
        rows = [
            compiled_row("required_input.annotation_gtf", "/formal/genome.gtf"),
            compiled_row("failure_policy.java_missing", "block", operator="on_condition", strength="conditional"),
            compiled_row("forbidden_behavior.species_database_guess", True, operator="forbid"),
        ]
        validation = validate_compiled_constraints(rows)
        spec = merge_compiled_constraints(spec, validation["constraints"], metrics=validation["metrics"])
        self.assertEqual(spec["goal"], raw)
        self.assertTrue(spec["source_refs"])
        self.assertNotIn("user_directives", spec)
        self.assertNotIn("semantic_compilation", spec)
        self.assertIn(
            "failure_policy.java_missing",
            {item["semantic_key"] for item in semantic_constraints_from_task_spec(spec)},
        )
        self.assertNotIn(raw, json.dumps(spec["requirements"], ensure_ascii=False))

    def test_session_1786157669_replay_goes_from_11_false_conflicts_to_zero(self):
        snapshot = json.loads(
            (FIXTURES / "session_1786157669_conflict_replay.json").read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot["session_id"], "session_1786157669")
        self.assertEqual(snapshot["audited_claim_count"], 37)
        self.assertEqual(snapshot["audited_before_user_plan_conflicts"], 11)
        self.assertEqual(len(snapshot["legacy_plan_propositions"]), 11)

        registry = ClaimRegistry()
        registry.register(
            snapshot["raw_prompt_provenance"],
            source_type="user_instruction",
            strength="hard_constraint",
            scope="task",
            scope_id=snapshot["session_id"],
            attributes={"provenance_only": True},
        )
        for index, proposition in enumerate(snapshot["legacy_plan_propositions"], 1):
            registry.register(
                proposition,
                source_type="plan",
                strength="required",
                scope="stage",
                scope_id=f"step-{index}",
            )
        after = [
            item for item in KnowledgeConflictDetector.detect(registry)
            if item.conflict_type == "USER_PLAN_CONFLICT"
        ]
        self.assertEqual(after, [])

        validation = validate_compiled_constraints(snapshot["semantic_constraints"])
        self.assertTrue(validation["valid"], validation["errors"])
        spec = merge_compiled_constraints(
            create_task_spec(snapshot["raw_prompt_provenance"], task_id=snapshot["session_id"]),
            validation["constraints"],
            metrics=validation["metrics"],
        )
        plan_state = {
            "task_id": snapshot["session_id"],
            "plan_version": "plan_v1",
            "task_spec": spec,
            "selected_skill_ids": ["genomics/snpeff-annotation"],
            "steps": [{
                "step_id": "step-1",
                "inputs": snapshot["legacy_plan_propositions"][1:4],
                "outputs": snapshot["legacy_plan_propositions"][4:],
                "skill_ids": ["genomics/snpeff-annotation"],
                "renderer_invocation_contract": snapshot["renderer_invocation"],
            }],
        }
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = "/nonexistent-read-only-replay"
        production_registry = agent._knowledge_claim_registry(
            snapshot["session_id"],
            {
                "task_spec": spec,
                "selected_skills": ["genomics/snpeff-annotation"],
                # This legacy field contained the complete raw prompt in the
                # audited session and must now be ignored by the comparator.
                "user_constraints": [snapshot["raw_prompt_provenance"]],
            },
            plan_state,
        )
        production_after = [
            item for item in KnowledgeConflictDetector.detect(production_registry)
            if item.conflict_type == "USER_PLAN_CONFLICT"
        ]
        self.assertEqual(production_after, [])

    def test_true_same_slot_conflicts_remain_blocking(self):
        cases = [
            ("required_input.reference_fasta", "/A.fa", "/B.fa", "eq", "eq"),
            ("required_behavior.database_source", "custom_genome", "preset", "eq", "eq"),
            ("required_behavior.annotation_format", "gtf22", "gff3", "eq", "eq"),
            ("forbidden_behavior.species_database_guess", True, True, "forbid", "eq"),
            ("forbidden_behavior.demo_or_toy_input", True, True, "forbid", "eq"),
        ]
        for key, expected, proposed, user_op, plan_op in cases:
            with self.subTest(key=key):
                registry = ClaimRegistry()
                register_typed(registry, "task_contract", key, expected, operator=user_op, strength="hard_constraint")
                register_typed(registry, "plan", key, proposed, operator=plan_op)
                conflicts = KnowledgeConflictDetector.detect(registry)
                self.assertEqual(len(conflicts), 1)
                self.assertEqual(conflicts[0].conflict_type, "USER_PLAN_CONFLICT")
                self.assertTrue(conflicts[0].blocking)

    def test_preference_mismatch_is_advisory_not_hard(self):
        registry = ClaimRegistry()
        register_typed(
            registry,
            "task_contract",
            "preference.database_source",
            "custom_genome",
            operator="prefer",
            strength="preferred",
        )
        register_typed(registry, "plan", "preference.database_source", "preset")
        conflict = KnowledgeConflictDetector.detect(registry)[0]
        self.assertEqual(conflict.conflict_type, "USER_PLAN_CONFLICT")
        self.assertFalse(conflict.blocking)

    def test_recovery_cannot_change_a_hard_semantic_slot(self):
        registry = ClaimRegistry()
        register_typed(
            registry,
            "task_contract",
            "required_behavior.annotation_format",
            "gtf22",
            strength="hard_constraint",
        )
        register_typed(registry, "recovery_proposal", "required_behavior.annotation_format", "gff3")
        conflict = KnowledgeConflictDetector.detect(registry)[0]
        self.assertEqual(conflict.conflict_type, "USER_RECOVERY_CONFLICT")
        self.assertTrue(conflict.blocking)

    def test_failure_policy_and_install_policy_are_deterministic(self):
        validation = validate_compiled_constraints([
            compiled_row(
                "failure_policy.java_missing",
                "block",
                operator="on_condition",
                strength="conditional",
                condition="java_missing",
            ),
        ])
        spec = merge_compiled_constraints(create_task_spec("runtime policy provenance"), validation["constraints"])
        self.assertTrue(evaluate_failure_policies(spec, {"java_missing": True})["blocked"])
        self.assertFalse(evaluate_failure_policies(spec, {"java_missing": False})["blocked"])
        self.assertFalse(semantic_install_policy(spec, dependency_key="java")["install_allowed"])

    def test_renderer_projection_emits_typed_behavior_claim(self):
        validation = validate_compiled_constraints([
            compiled_row("required_input.annotation_gtf", "/formal/genome.gtf"),
            compiled_row("required_behavior.annotation_format", "gtf22"),
        ])
        spec = merge_compiled_constraints(create_task_spec("provenance"), validation["constraints"])
        projection = project_task_spec_to_renderer(
            spec,
            {
                "arguments": {"annotation_gtf": {"required": True}},
                "routed_arguments": {
                    "annotation_file": {
                        "selector": "annotation_format",
                        "routes": {"gtf22": "annotation_gtf"},
                    }
                },
            },
        )
        self.assertTrue(projection["complete"], projection)
        self.assertIn(
            "required_behavior.annotation_format",
            {item["semantic_key"] for item in projection["semantic_claims"]},
        )

    def test_compiled_output_directory_binds_as_directory_not_filename(self):
        validation = validate_compiled_constraints([
            compiled_row("required_output.output_dir", "/formal/output")
        ])
        spec = merge_compiled_constraints(create_task_spec("provenance"), validation["constraints"])
        projection = project_task_spec_to_renderer(
            spec,
            {"arguments": {"output_dir": {"required": True}}},
        )
        self.assertTrue(projection["complete"], projection)
        self.assertEqual(projection["outdir"], "/formal/output")
        self.assertEqual(
            next(
                item for item in spec["output_contracts"]
                if item["role"] == "output_dir"
            )["canonical_path"],
            "/formal/output",
        )

    def test_renderer_binding_removes_plan_parser_garbage(self):
        plan = """## Execution Checklist
- [ ] [step-1] Run annotation
  - Action: EXECUTE
  - Purpose: analysis
  - Skills: [genomics/snpeff-annotation]
  - Inputs:
    - **
    - ** none
    - VCF
    - FASTA
    - GTF 文件
    - Java 路径
  - Outputs:
    - annotated.vcf
  - Depends on: none
  - Success criteria: annotated VCF exists
  - Stage: none
  - Batch: none
"""
        parsed = parse_plan_steps(plan)
        self.assertNotIn("**", parsed[0]["inputs"])
        self.assertNotIn("** none", parsed[0]["inputs"])
        self.assertFalse({"VCF", "FASTA", "GTF 文件", "Java 路径"} & set(parsed[0]["inputs"]))
        parsed[0].update({
            "step_type": "renderer_composite",
            "selected_skill_id": "genomics/snpeff-annotation",
            "renderer_id": "genomics.snpeff_annotation",
        })
        spec = merge_task_spec(
            create_task_spec("provenance"),
            {
                "inputs": [
                    {"role": "variant_vcf", "path": "/formal/input.vcf"},
                    {"role": "reference_fasta", "path": "/formal/genome.fasta"},
                    {"role": "annotation_gtf", "path": "/formal/genome.gtf"},
                ],
                "requirements": [{
                    "requirement_id": "database_source",
                    "role": "database_source",
                    "value": "custom_genome",
                    "level": "required",
                }],
            },
            source="consultant_inferred",
            reason="test",
        )
        state = {"task_id": "task", "steps": parsed}
        records = [{
            "skill_id": "genomics/snpeff-annotation",
            "renderer_id": "genomics.snpeff_annotation",
                "invocation": {
                    "binding_rules": {
                        "genome_version": {
                            "owner": "task_spec",
                            "repair_target": "task_spec.requirements[role=database_source]",
                            "sources": [{
                                "kind": "stable_task_identifier",
                                "role": "database_source",
                                "prefix": "biomind",
                                "rule_id": "test.custom_database.task_id.v1",
                                "when": {
                                    "role": "database_source",
                                    "operator": "eq",
                                    "value": "custom_genome",
                                },
                            }],
                        },
                    },
                    "arguments": {
                    "vcf": {"required": True},
                    "reference_fasta": {"required": True},
                    "annotation_gtf": {"required": True},
                    "genome_version": {"required": True},
                }
            },
        }]
        bound = bind_renderer_invocation_contracts(state, records, spec)
        self.assertEqual(
            bound["steps"][0]["inputs"],
            ["/formal/input.vcf", "/formal/genome.fasta", "/formal/genome.gtf"],
        )
        self.assertFalse({"VCF", "FASTA", "GTF 文件", "Java 路径"} & set(bound["steps"][0]["inputs"]))

    def test_inline_semantic_claim_json_stops_before_later_bracket_fields(self):
        claim = compiled_row("required_input.variant_vcf", "/formal/input.vcf")
        plan = (
            "## Execution Checklist\n"
            "- [ ] Step 1: annotate | Action: EXECUTE | "
            "Semantic claims: " + json.dumps([claim], ensure_ascii=False) + " | "
            "Depends on: [none] | Outputs: [out.vcf] | Done: output exists\n"
        )
        parsed = parse_plan_steps(plan)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(parsed[0]["semantic_claims"]), 1)
        self.assertEqual(
            parsed[0]["semantic_claims"][0]["semantic_key"],
            "required_input.variant_vcf",
        )
        self.assertEqual(parsed[0]["semantic_claims"][0]["value"], "/formal/input.vcf")
        self.assertEqual(parsed[0]["outputs"], ["out.vcf"])

    def test_approved_no_delta_reuses_semantic_snapshot(self):
        validation = validate_compiled_constraints([
            compiled_row("required_input.variant_vcf", "/formal/input.vcf")
        ])
        spec = merge_compiled_constraints(create_task_spec("provenance"), validation["constraints"])
        state = build_plan_state(
            "task",
            "- [ ] Step 1: run | Action: EXECUTE | Inputs: /formal/input.vcf | Outputs: out.vcf | Done: output exists",
        )
        state["task_spec"] = spec
        state["canonical_constraint_hash"] = canonical_constraint_hash(spec)
        registry = ClaimRegistry()
        register_typed(registry, "task_contract", "required_input.variant_vcf", "/formal/input.vcf", strength="hard_constraint")
        register_typed(registry, "plan", "required_input.variant_vcf", "/formal/input.vcf")
        state["knowledge_claim_set_hash"] = semantic_claim_set_hash(registry)
        approve_plan_state(state, 1)
        unchanged = approval_semantic_delta(
            state,
            current_constraint_hash=canonical_constraint_hash(spec),
            current_claim_set_hash=semantic_claim_set_hash(registry),
        )
        changed = approval_semantic_delta(
            state,
            current_constraint_hash=canonical_constraint_hash(spec),
            current_claim_set_hash="post-approval-new-fact",
        )
        self.assertFalse(unchanged["changed"], unchanged)
        self.assertTrue(changed["changed"])
        self.assertIn("semantic_claim_set_changed", changed["reasons"])


if __name__ == "__main__":
    unittest.main()
