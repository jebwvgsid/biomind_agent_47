from __future__ import annotations

import asyncio
import hashlib
import json
import unittest
from dataclasses import replace

from agent_core.workflow.context_compaction import ActiveEpochPointer, CompactionPlan, ContextBudgetPolicy, ContextCompactionAttemptsExhausted, ContextCompactionError, ContextEpochCASConflict, ReferenceTokenEstimator, SourceUnit, TokenEstimate, activate_epoch_cas, assemble_compacted_epoch, build_source_unit_manifest, measure_provider_input, new_compaction_request, resolve_source_body, segment_source_unit_losslessly, validate_compaction_plan
from prompts.context_compaction import (
    CONTEXT_COMPACTION_PROMPT_ID,
    CONTEXT_COMPACTION_SYSTEM_PROMPT,
    build_context_compaction_messages,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _CodepointEstimator:
    """Predictable exact fixture counter; never used by production defaults."""

    name = "fixture_codepoints"
    version = "1"
    exact_or_estimated = "exact"

    def estimate(self, text: str) -> TokenEstimate:
        return TokenEstimate(
            tokens=len(text),
            estimator_name=self.name,
            estimator_version=self.version,
            exact_or_estimated=self.exact_or_estimated,
        )


class ContextCompactionFixture(unittest.TestCase):
    epoch_id = "epoch-source-1"
    history_version = 7
    selected_revision = 3

    def unit(
        self,
        unit_id: str,
        body: str,
        *,
        selected: bool = False,
        skill_id: str = "",
        protocol_group_id: str = "",
        kind: str = "message",
    ) -> tuple[SourceUnit, str]:
        ref = f"immutable:test:{unit_id}"
        unit = SourceUnit(
            unit_id=unit_id,
            kind=kind,
            source_ref=ref,
            body_ref=ref,
            content_hash=_sha(body),
            coverage={"kind": "full"},
            scope={
                "task_id": "task-1",
                "phase": "planner",
                "epoch_id": self.epoch_id,
                "history_version": self.history_version,
                "selected_revision": self.selected_revision,
            },
            token_count=len(body),
            body_length_chars=len(body),
            selected=selected,
            skill_id=skill_id,
            protocol_group_id=protocol_group_id,
            provenance={"producer": "fixture"},
        )
        return unit, body

    def request(
        self,
        unit_bodies: list[tuple[SourceUnit, str]],
        *,
        policy: ContextBudgetPolicy | None = None,
        fixed_components=None,
    ):
        manifest = build_source_unit_manifest(
            source_epoch_id=self.epoch_id,
            history_version=self.history_version,
            selected_revision=self.selected_revision,
            units=[unit for unit, _body in unit_bodies],
        )
        request = new_compaction_request(
            manifest,
            budget_policy=policy,
            fixed_components=fixed_components or {},
        )
        resolver = {
            unit.body_ref: body for unit, body in unit_bodies
        }
        return request, resolver

    @staticmethod
    def plan(
        request,
        *,
        retain=(),
        summarized=(),
        mode="normal",
        summary="working summary",
        recovery=(),
        unresolved=(),
        **extra,
    ):
        return {
            "schema_version": 1,
            "compaction_request_id": request.compaction_request_id,
            "source_epoch_id": request.source_epoch_id,
            "source_manifest_hash": request.source_manifest_hash,
            "mode": mode,
            "retain_exact_unit_ids": list(retain),
            "summary_source_unit_ids": list(summarized),
            "summary": summary if summarized else "",
            "material_retrieval_state": list(recovery),
            "unresolved_items": list(unresolved),
            "selection_rationale": "fixture selection",
            **extra,
        }


class ContextBudgetTests(ContextCompactionFixture):
    def test_application_budget_is_explicit_and_provider_limit_is_a_minimum(self) -> None:
        default = ContextBudgetPolicy()
        self.assertEqual(default.application_context_limit_tokens, 312_144)
        self.assertEqual(default.effective_context_limit_tokens, 312_144)
        self.assertIsNone(default.known_provider_context_limit_tokens)

        bounded = ContextBudgetPolicy(
            known_provider_context_limit_tokens=128_000,
            output_reserve_tokens=8_000,
            estimation_margin_tokens=2_000,
            safety_headroom_tokens=1_000,
        )
        self.assertEqual(bounded.effective_context_limit_tokens, 128_000)
        self.assertEqual(bounded.safe_input_tokens, 118_000)
        self.assertEqual(
            bounded.trigger_tokens,
            min(128_000, 117_000),
        )

    def test_reference_estimate_is_labeled_and_counts_every_component(self) -> None:
        snapshot = measure_provider_input(
            {
                "system": "规则",
                "tool_schema": {"工具": ["读取"]},
                "frozen_inputs": "/很/长/的/路径/input.vcf",
                "inherited_materials": "print({'基因': 1})",
                "local_messages": [{"role": "human", "content": "继续"}],
                "pending_results": {"status": "完整"},
                "provider_wrappers": "<wrapper/>",
            },
            cached_input_tokens=123,
        )
        self.assertIn("reference", snapshot.estimator_name)
        self.assertEqual(snapshot.exact_or_estimated, "estimated")
        self.assertEqual(snapshot.cached_input_tokens, 123)
        self.assertTrue(snapshot.to_dict()["cached_tokens_count_toward_input"])
        self.assertEqual(
            set(snapshot.component_token_counts),
            {
                "system", "tool_schema", "frozen_inputs",
                "inherited_materials", "local_messages",
                "pending_results", "provider_wrappers",
            },
        )
        self.assertEqual(
            sum(snapshot.component_token_counts.values()),
            snapshot.estimated_input_tokens,
        )

    def test_cached_usage_is_telemetry_not_a_capacity_discount(self) -> None:
        components = {"system": "x" * 300}
        without_cache = measure_provider_input(components)
        with_cache = measure_provider_input(
            components, cached_input_tokens=without_cache.estimated_input_tokens
        )
        self.assertEqual(
            with_cache.estimated_input_tokens,
            without_cache.estimated_input_tokens,
        )


class SourceUnitTests(ContextCompactionFixture):
    def test_manifest_rejects_body_duplication_and_scope_drift(self) -> None:
        unit, body = self.unit("u1", "exact")
        raw = unit.to_dict()
        raw["body"] = body
        with self.assertRaisesRegex(
            ContextCompactionError, "CONTEXT_SCHEMA_UNKNOWN_FIELD"
        ):
            SourceUnit.from_mapping(raw)

        raw = unit.to_dict()
        raw["scope"]["selected_revision"] = 99
        drifted = SourceUnit.from_mapping(raw)
        with self.assertRaisesRegex(
            ContextCompactionError, "SOURCE_UNIT_SCOPE_MISMATCH"
        ):
            build_source_unit_manifest(
                source_epoch_id=self.epoch_id,
                history_version=self.history_version,
                selected_revision=self.selected_revision,
                units=[drifted],
            )

    def test_body_resolution_is_hash_and_length_checked(self) -> None:
        unit, body = self.unit("u1", "immutable exact bytes")
        self.assertEqual(resolve_source_body(unit, {unit.body_ref: body}), body)
        with self.assertRaisesRegex(
            ContextCompactionError, "SOURCE_BODY_HASH_MISMATCH"
        ):
            resolve_source_body(unit, {unit.body_ref: body + "!"})

    def test_oversize_unicode_source_is_losslessly_range_segmented(self) -> None:
        body = "第一行\nprint({'基因': 1})\n" + "ACGT" * 73 + "\n尾部哨兵"
        unit, _ = self.unit(
            "oversize", body, selected=True,
            skill_id="genomics/example", kind="skill_document",
        )
        result = segment_source_unit_losslessly(
            unit,
            body,
            max_segment_tokens=37,
            estimator=_CodepointEstimator(),
        )
        self.assertGreater(len(result.segments), 1)
        self.assertEqual(result.reconstruct(), body)
        self.assertEqual(_sha(result.reconstruct()), unit.content_hash)
        cursor = 0
        for segment in result.segments:
            coverage = segment.coverage
            self.assertEqual(coverage["start_offset"], cursor)
            cursor = coverage["end_offset"]
            self.assertLessEqual(segment.token_count, 37)
            self.assertTrue(segment.selected)
            self.assertEqual(segment.skill_id, unit.skill_id)
        self.assertEqual(cursor, len(body))

    def test_protocol_group_identity_survives_segmentation(self) -> None:
        unit, body = self.unit(
            "tool-result", "result" * 40, protocol_group_id="tool-group-1"
        )
        result = segment_source_unit_losslessly(
            unit,
            body,
            max_segment_tokens=25,
            estimator=_CodepointEstimator(),
        )
        self.assertEqual(
            {item.protocol_group_id for item in result.segments},
            {"tool-group-1"},
        )


class CompactionPlanTests(ContextCompactionFixture):
    def test_every_unit_has_exactly_one_destination(self) -> None:
        first, first_body = self.unit("u1", "one")
        second, second_body = self.unit("u2", "two")
        request, _resolver = self.request(
            [(first, first_body), (second, second_body)]
        )
        with self.assertRaisesRegex(
            ContextCompactionError,
            "COMPACTION_SOURCE_UNIT_DESTINATION_MISSING",
        ):
            validate_compaction_plan(
                self.plan(request, retain=["u1"]), request
            )
        with self.assertRaisesRegex(
            ContextCompactionError,
            "COMPACTION_SOURCE_UNIT_MULTIPLE_DESTINATIONS",
        ):
            validate_compaction_plan(
                self.plan(
                    request,
                    retain=["u1", "u2"],
                    summarized=["u2"],
                ),
                request,
            )

    def test_invalid_identity_and_structured_state_mutation_fail_closed(self) -> None:
        unit, body = self.unit("u1", "one")
        request, _resolver = self.request([(unit, body)])
        wrong_hash = self.plan(request, retain=["u1"])
        wrong_hash["source_manifest_hash"] = "0" * 64
        with self.assertRaisesRegex(
            ContextCompactionError, "COMPACTION_SOURCE_MANIFEST_MISMATCH"
        ):
            validate_compaction_plan(wrong_hash, request)

        route = self.plan(request, retain=["u1"])
        route["unresolved_items"] = [{"next_step": "execute"}]
        with self.assertRaisesRegex(
            ContextCompactionError,
            "COMPACTION_STATE_MUTATION_FIELD_FORBIDDEN",
        ):
            CompactionPlan.from_mapping(route)

        copied_body = self.plan(request, retain=["u1"], body="forged")
        with self.assertRaisesRegex(
            ContextCompactionError, "CONTEXT_SCHEMA_UNKNOWN_FIELD"
        ):
            CompactionPlan.from_mapping(copied_body)

    def test_tool_protocol_group_cannot_be_split(self) -> None:
        call, call_body = self.unit(
            "tool-call", "call", protocol_group_id="g1",
            kind="assistant_tool_call",
        )
        result, result_body = self.unit(
            "tool-result", "result", protocol_group_id="g1",
            kind="tool_result",
        )
        request, _resolver = self.request(
            [(call, call_body), (result, result_body)]
        )
        with self.assertRaisesRegex(
            ContextCompactionError, "COMPACTION_PROTOCOL_GROUP_SPLIT"
        ):
            validate_compaction_plan(
                self.plan(
                    request,
                    retain=["tool-call"],
                    summarized=["tool-result"],
                ),
                request,
            )

    def test_selected_material_requires_pressure_mode_and_recovery_state(self) -> None:
        unit, body = self.unit(
            "skill", "mutable workspace: inspect live state",
            selected=True, skill_id="core/workspace",
            kind="skill_document",
        )
        request, _resolver = self.request([(unit, body)])
        with self.assertRaisesRegex(
            ContextCompactionError,
            "COMPACTION_SELECTED_SUMMARY_REQUIRES_CAPACITY_PRESSURE",
        ):
            validate_compaction_plan(
                self.plan(request, summarized=["skill"]), request
            )
        with self.assertRaisesRegex(
            ContextCompactionError,
            "COMPACTION_SELECTED_RECOVERY_STATE_REQUIRED",
        ):
            validate_compaction_plan(
                self.plan(
                    request,
                    summarized=["skill"],
                    mode="capacity_pressure",
                ),
                request,
            )

        recovery = {
            "source_unit_id": unit.unit_id,
            "skill_id": unit.skill_id,
            "source_ref": unit.source_ref,
            "content_hash": unit.content_hash,
            "coverage": unit.to_dict()["coverage"],
            "scope": unit.to_dict()["scope"],
            "residency": "summarized",
            "reason": "exact text exceeds safe capacity",
            "summary": "workspace is mutable",
            "missing_details": "exact procedure is not resident",
            "retrieval_ref": unit.body_ref,
        }
        valid = validate_compaction_plan(
            self.plan(
                request,
                summarized=["skill"],
                mode="capacity_pressure",
                recovery=[recovery],
            ),
            request,
        )
        self.assertEqual(valid.mode, "capacity_pressure")


class AssemblyAndAttemptTests(ContextCompactionFixture):

    def test_runtime_preserves_manifest_order_not_model_id_order(self) -> None:
        first, first_body = self.unit("first", "A")
        second, second_body = self.unit("second", "B")
        request, resolver = self.request(
            [(first, first_body), (second, second_body)]
        )
        prepared = assemble_compacted_epoch(
            self.plan(request, retain=["second", "first"]),
            request,
            resolver,
        )
        self.assertEqual(
            [item.unit.unit_id for item in prepared.retained_materials],
            ["first", "second"],
        )
        self.assertEqual(
            prepared.plan.retain_exact_unit_ids,
            ("first", "second"),
        )

    def test_retain_ids_move_original_body_without_model_copy(self) -> None:
        sentinel = "middle\n" + "长脚本" * 500 + "\nTAIL_SENTINEL"
        unit, body = self.unit(
            "script", sentinel, selected=True,
            skill_id="example/skill", kind="script",
        )
        request, resolver = self.request([(unit, body)])
        plan = self.plan(request, retain=["script"])
        prepared = assemble_compacted_epoch(plan, request, resolver)
        self.assertEqual(prepared.retained_materials[0].body, body)
        self.assertEqual(
            _sha(prepared.retained_materials[0].body), unit.content_hash
        )
        archive_json = json.dumps(
            prepared.to_archive_payload(), ensure_ascii=False
        )
        self.assertNotIn("TAIL_SENTINEL", archive_json)
        provider_materials = prepared.provider_materials()
        self.assertEqual(provider_materials[0]["body"], body)

    def test_target_is_not_a_python_pin_when_selection_still_fits_safe_input(self) -> None:
        unit, body = self.unit(
            "skill", "x" * 3_000, selected=True,
            skill_id="example/skill", kind="skill_document",
        )
        policy = ContextBudgetPolicy(
            application_context_limit_tokens=8_000,
            output_reserve_tokens=500,
            estimation_margin_tokens=200,
            safety_headroom_tokens=100,
            trigger_ratio=0.85,
            target_ratio=0.30,
        )
        request, resolver = self.request([(unit, body)], policy=policy)
        prepared = assemble_compacted_epoch(
            self.plan(request, retain=["skill"]),
            request,
            resolver,
            estimator=_CodepointEstimator(),
        )
        self.assertGreater(
            prepared.budget.estimated_input_tokens, policy.target_tokens
        )
        self.assertLessEqual(
            prepared.budget.estimated_input_tokens, policy.safe_input_tokens
        )
        self.assertEqual(
            prepared.plan.retain_exact_unit_ids, ("skill",)
        )





class EpochTransactionTests(ContextCompactionFixture):
    def _prepared_epoch(self):
        unit, body = self.unit("u1", "exact body")
        request, resolver = self.request([(unit, body)])
        request = replace(request, source_archive_ref="archive:source")
        prepared = assemble_compacted_epoch(
            self.plan(request, retain=["u1"]), request, resolver
        )
        return request, resolver, prepared

    def test_pure_cas_conflict_leaves_old_pointer_unchanged(self) -> None:
        request, _resolver, prepared = self._prepared_epoch()
        old = ActiveEpochPointer(
            state_generation=10,
            epoch_id=request.source_epoch_id,
            epoch_archive_ref="archive:old",
            history_version=request.history_version + 1,
            selected_revision=request.selected_revision,
        )
        snapshot = old
        with self.assertRaisesRegex(
            ContextEpochCASConflict, "CONTEXT_EPOCH_CAS_CONFLICT"
        ):
            activate_epoch_cas(
                old,
                expected_state_generation=10,
                expected_epoch_id=request.source_epoch_id,
                expected_history_version=request.history_version,
                expected_selected_revision=request.selected_revision,
                candidate=prepared,
                candidate_epoch_archive_ref="archive:candidate",
            )
        self.assertIs(old, snapshot)
        self.assertEqual(old.epoch_archive_ref, "archive:old")

    def test_post_cas_replay_is_idempotently_recognized(self) -> None:
        request, _resolver, prepared = self._prepared_epoch()
        already_active = ActiveEpochPointer(
            state_generation=11,
            epoch_id=prepared.record.epoch_id,
            epoch_archive_ref="archive:candidate",
            history_version=request.history_version,
            selected_revision=request.selected_revision,
        )
        receipt = activate_epoch_cas(
            already_active,
            expected_state_generation=10,
            expected_epoch_id=request.source_epoch_id,
            expected_history_version=request.history_version,
            expected_selected_revision=request.selected_revision,
            candidate=prepared,
            candidate_epoch_archive_ref="archive:candidate",
        )
        self.assertTrue(receipt.already_active)
        self.assertIs(receipt.active, already_active)





class PromptContractTests(ContextCompactionFixture):
    def test_prompt_is_versioned_complete_and_denies_authority(self) -> None:
        unit, body = self.unit("u1", "SENTINEL_FULL_BODY")
        request, resolver = self.request(
            [(unit, body)],
            fixed_components={
                "system": {"content": "SENTINEL_FIXED_SYSTEM"},
                "frozen_inputs": {"content": "SENTINEL_FIXED_SEED"},
            },
        )
        messages = build_context_compaction_messages(
            request.to_prompt_payload(resolver),
            attempt_ordinal=2,
            capacity_feedback={"over_by_tokens": 42},
        )
        self.assertEqual([item["role"] for item in messages], ["system", "human"])
        self.assertIn(CONTEXT_COMPACTION_PROMPT_ID, messages[1]["content"])
        self.assertIn("SENTINEL_FULL_BODY", messages[1]["content"])
        self.assertIn("SENTINEL_FIXED_SYSTEM", messages[1]["content"])
        self.assertIn("SENTINEL_FIXED_SEED", messages[1]["content"])
        self.assertIn('"over_by_tokens":42', messages[1]["content"])
        self.assertIn("never execution evidence", CONTEXT_COMPACTION_SYSTEM_PROMPT)
        self.assertIn("retain_exact_unit_ids", CONTEXT_COMPACTION_SYSTEM_PROMPT)
        self.assertIn("capacity_pressure", CONTEXT_COMPACTION_SYSTEM_PROMPT)
        self.assertIn("cannot be retained, summarized", CONTEXT_COMPACTION_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
