import json
import unittest
from copy import deepcopy
from pathlib import Path

from agent_core.consultant_finalize import (
    build_finalize_batch_messages,
    build_finalize_batch_schema,
    build_finalize_candidate_catalog,
    build_finalize_decision_batch,
    build_finalize_work_preflight,
    build_partition_capacity_basis,
    classify_directive_coverage,
    compile_deterministic_coverage,
    finalize_batch_llm_payload,
    merge_finalize_batch_decisions,
    stable_partition_finalize_batches,
    strict_decode_finalize_batch_response,
    validate_finalize_batch_response,
)


SOURCE_ID = "msg-e59c6c72c2954e5c7349"
SKILL_ID = "genomics/snpeff-annotation"


def _constraint(key, operator, value, *, source_id=SOURCE_ID, strength="hard"):
    return {
        "semantic_key": key,
        "operator": operator,
        "value": value,
        "strength": strength,
        "confidence": 1.0,
        "provenance": [{"source_type": "user_explicit", "source_id": source_id}],
    }


def _incident_context():
    input_paths = [
        "benchmarks/01.测试/04.重测序/Part2.vairant_annotation/data/all.filtered.snp.vcf",
        "benchmarks/01.测试/04.重测序/Part2.vairant_annotation/data/genome.fasta",
        "benchmarks/01.测试/04.重测序/Part2.vairant_annotation/data/genome.gtf",
    ]
    outputs = [
        "annotated.vcf",
        "snpEff_summary.html",
        "ann_effect_summary.tsv",
        "ann_gene_summary.tsv",
        "report.md",
        "reproduce.sh",
        "stage_status.tsv",
    ]
    output_dir = "benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-ANNOT-007/strict"
    base = [
        _constraint("required_input.variant_vcf", "eq", input_paths[0]),
        _constraint("required_input.reference_fasta", "eq", input_paths[1]),
        _constraint("required_input.annotation_gtf", "eq", input_paths[2]),
        *[
            _constraint(f"required_output.output_{index}", "eq", value)
            for index, value in enumerate(outputs)
        ],
        _constraint("required_output.output_dir", "eq", output_dir),
        _constraint("parameter.cli.gtf22", "eq", True),
    ]
    assert len(base) == 12
    base_directives = []
    for index, row in enumerate(base):
        base_directives.append({
            "directive_id": f"base-{index:02d}",
            "directive_kind": row["semantic_key"].split(".", 1)[0],
            "semantic_key": row["semantic_key"],
            "operator": row["operator"],
            "normalized_value": deepcopy(row["value"]),
            "normalized_text": str(row["value"]),
            "strength": row["strength"],
            "source_ids": [SOURCE_ID],
        })

    clauses = [
        "请用 genomics/snpeff-annotation 给 all.filtered.snp.vcf 做 custom genome 注释。",
        "必须使用 data/genome.fasta 和 genome.gtf，以 -gtf22 构建/验证本地 snpEff database，不要猜物种数据库；",
        "输出 annotated.vcf、snpEff_summary.html、ann_effect_summary.tsv、report.md、reproduce.sh。",
        "建议/目标 skills：",
        SKILL_ID,
        "输入文件或数据位置：",
        *input_paths,
        "输出目录：",
        output_dir,
        "允许：",
        "允许渲染 snpEff database build 脚本。",
        "允许 blocked if snpEff/java missing。",
        "禁止：",
        "禁止使用 tests/demo_ref.fa 或 demo.vcf。",
        "禁止用 taxonomy label 猜 public snpEff DB。",
        "禁止忽略 chromosome naming mismatch。",
        "依赖规则：",
        "snpEff/Java/database build 失败时 blocked；",
        "不得输出假 ANN。",
        "停止条件：",
        "生成 annotated VCF 和 ANN summary，或 blocked。",
        "预期产物：",
        *outputs[:6],
        "通用要求：",
        "只使用本提示词给出的正式输入或数据目录；",
        "不得把 BioMind 代码仓库目录误当作 benchmark 数据根目录。",
        "所有 `benchmarks/...` 相对路径都以项目根目录解析；",
        "如当前工作目录不同，请先 `cd` 到项目根目录或改用绝对路径。",
        "不要使用 demo、toy、tests、fixtures_legacy_toy、expected_answers 或任何隐藏答案文件作为分析依据。",
        "缺少或无法确认关键输入、样本方向、contrast、resolution、阈值或项目背景时，先做只读验证和正式产物搜索；",
        "仍不确定则 ask_user/blocked_need_user_decision，不要猜测。",
        "若本提示词明确标注 `KNOWN_ABSENT_FOR_THIS_TASK`，则该输入已知不提供；",
        "不要反复询问或借用其他 benchmark/hidden/fixture 文件，应按本任务规则写 skipped/stage_blocked 并继续可执行步骤。",
        "如果依赖、输入或上游结果不足，请明确写 blocked/stage_blocked，并说明缺少什么；",
        "不要伪造结果。",
        "最终至少生成 report.md 和 reproduce.sh；",
        "多阶段任务还应生成 stage_status.tsv 或等价状态表。",
    ]
    assert len(clauses) == 44
    envelope_id = "request-envelope-incident"
    envelope = {
        "directive_id": envelope_id,
        "directive_kind": "request_envelope",
        "normalized_text": "\n".join(clauses),
        "strength": "source_authority",
        "source_ids": [SOURCE_ID],
    }
    segment_directives = []
    gaps = []
    for index, value in enumerate(clauses):
        forbid = value.startswith(("禁止", "不得", "不要"))
        directive_id = f"{envelope_id}:segment:{index:02d}"
        category = "forbidden_behavior" if forbid else "required_behavior"
        operator = "forbid" if forbid else "require"
        segment_directives.append({
            "directive_id": directive_id,
            "directive_kind": category,
            "semantic_key": f"{category}.user_directive_{index:040x}",
            "operator": operator,
            "normalized_value": value,
            "normalized_text": value,
            "strength": "hard",
            "source_ids": [SOURCE_ID],
        })
        gaps.append({
            "directive_id": directive_id,
            "semantic_key": f"{category}.user_directive_{index:040x}",
            "operator": operator,
            "normalized_value": value,
            "strength": "hard",
            "condition": None,
            "source_ids": [SOURCE_ID],
        })
    return {
        "finalize_context_id": "finalize:session-1786266235",
        "base_contract_hash": "base-contract-1786266235",
        "resolved_selected_skill_ids": [SKILL_ID],
        "semantic_provenance_catalog": [{
            "source_id": SOURCE_ID,
            "source_type": "user_explicit",
        }],
        "deterministic_base_constraints": base,
        "coverage_gaps": gaps,
        "unresolved_semantics": [],
        "effective_task_contract": {
            "required_inputs": [
                {
                    "fact_id": f"input-{index}",
                    "path": value,
                    "source_type": "user_explicit",
                    "source_id": SOURCE_ID,
                }
                for index, value in enumerate(input_paths)
            ],
            "required_outputs": [
                {
                    "output_id": f"output-{index}",
                    "filename": value,
                    "source_type": "user_explicit",
                    "source_id": SOURCE_ID,
                }
                for index, value in enumerate(outputs)
            ] + [{
                "output_id": "output-dir",
                "path": output_dir,
                "source_type": "user_explicit",
                "source_id": SOURCE_ID,
            }],
            "parameters": [{
                "fact_id": "gtf22",
                "namespace": "cli",
                "key": "gtf22",
                "value": True,
                "source_type": "user_explicit",
                "source_id": SOURCE_ID,
            }],
            "decisions": [],
            "source_addressable_directives": [
                *base_directives,
                envelope,
                *segment_directives,
            ],
        },
    }


def _model_context(item_count=1):
    directives = []
    gaps = []
    for index in range(item_count):
        directive_id = f"ambiguous-{index}"
        directives.append({
            "directive_id": directive_id,
            "directive_kind": "required_behavior",
            "semantic_key": f"required_behavior.user_directive_{index:040x}",
            "operator": "require",
            "normalized_value": f"Choose interpretation {index}",
            "normalized_text": f"Choose interpretation {index}",
            "strength": "hard",
            "source_ids": [SOURCE_ID],
            "needs_model_resolution": True,
        })
        gaps.append({
            "directive_id": directive_id,
            "semantic_key": f"required_behavior.user_directive_{index:040x}",
            "operator": "require",
            "normalized_value": f"Choose interpretation {index}",
            "strength": "hard",
            "condition": None,
            "source_ids": [SOURCE_ID],
        })
    return {
        "finalize_context_id": "finalize:model-test",
        "base_contract_hash": "base:model-test",
        "resolved_selected_skill_ids": [SKILL_ID],
        "semantic_provenance_catalog": [{
            "source_id": SOURCE_ID,
            "source_type": "user_explicit",
        }],
        "deterministic_base_constraints": [],
        "coverage_gaps": gaps,
        "unresolved_semantics": [],
        "effective_task_contract": {
            "required_inputs": [],
            "required_outputs": [],
            "parameters": [],
            "decisions": [],
            "source_addressable_directives": directives,
        },
    }


def _catalog_for_context(context):
    coverage = classify_directive_coverage(context)
    candidates = {}
    for item_index, item in enumerate(coverage["model_resolution_items"]):
        resolution_id = item["resolution_id"]
        candidates[resolution_id] = [
            {
                "candidate_id": f"candidate-{item_index}-a",
                "meaning": f"First bounded interpretation for item {item_index}",
                "normalized_value": f"value-{item_index}-a",
                "constraint": _constraint(
                    f"required_behavior.choice_{item_index}",
                    "eq",
                    f"value-{item_index}-a",
                ),
            },
            {
                "candidate_id": f"candidate-{item_index}-b",
                "meaning": f"Second bounded interpretation for item {item_index}",
                "normalized_value": f"value-{item_index}-b",
                "constraint": _constraint(
                    f"required_behavior.choice_{item_index}",
                    "eq",
                    f"value-{item_index}-b",
                ),
            },
        ]
    return coverage, build_finalize_candidate_catalog(coverage, candidates)


def _valid_payload(batch, candidate_index=0):
    visible = finalize_batch_llm_payload(batch)
    return {
        "action_type": "resolve_batch",
        "resolutions": [
            {
                "resolution_handle": item["resolution_handle"],
                "candidate_handle": item["candidates"][candidate_index]["candidate_handle"],
            }
            for item in visible["items"]
        ],
    }


class ConsultantFinalizeV4CoverageTests(unittest.TestCase):
    def test_session_1786266235_shape_compiles_without_provider_call(self):
        context = _incident_context()
        coverage = classify_directive_coverage(context)

        self.assertEqual(coverage["directive_count"], 57)
        self.assertEqual(len(context["coverage_gaps"]), 44)
        self.assertEqual(coverage["counts"]["covered_by_base"], 12)
        self.assertEqual(coverage["counts"]["provenance_envelope"], 1)
        self.assertEqual(coverage["counts"]["structural_only"], 9)
        self.assertEqual(coverage["counts"]["typed_fact_alias"], 11)
        self.assertEqual(coverage["counts"]["model_resolution_required"], 0)

        compiled = compile_deterministic_coverage(context, coverage)
        self.assertEqual(compiled["provider_status"], "not_called")
        self.assertEqual(compiled["completion_integrity"], "not_called")
        self.assertEqual(compiled["protocol_status"], "valid")
        self.assertEqual(compiled["next_step"], "planner")
        self.assertEqual(compiled["compiled_contract"]["selected_skills"], [SKILL_ID])
        serialized = json.dumps(
            compiled["compiled_contract"]["constraints"],
            ensure_ascii=False,
            sort_keys=True,
        )
        for required in (
            "-gtf22",
            "不要猜物种数据库",
            "不得输出假 ANN",
            "chromosome naming mismatch",
            "annotated.vcf",
            "stage_status.tsv",
        ):
            self.assertIn(required, serialized)
        synthetic_constraints = [
            item for item in compiled["compiled_contract"]["constraints"]
            if ".directive_" in str(item.get("semantic_key") or "")
        ]
        self.assertTrue(synthetic_constraints)
        self.assertTrue(all(
            item.get("enforcement_scope") == "runtime_global"
            for item in synthetic_constraints
        ))

    def test_real_session_1786266235_plan_state_replays_deterministically(self):
        run_state_path = (
            Path(__file__).resolve().parents[2]
            / "task_outputs"
            / "session_1786266235"
            / "plan_state.json"
        )
        if not run_state_path.is_file():
            self.skipTest("real session_1786266235 plan_state fixture is unavailable")

        # Import lazily so the pure-protocol tests do not otherwise depend on
        # the application bootstrap.  The replay invokes only context builders;
        # it cannot execute the historical healthcheck or call a provider.
        from bio_agents import BioAnalystAgent

        persisted = json.loads(run_state_path.read_text(encoding="utf-8"))
        task_state = persisted["task_state"]
        consultation = task_state["consultation_state"]
        selected_skills = list(
            consultation.get("resolved_selected_skill_ids") or []
        )
        agent = object.__new__(BioAnalystAgent)
        finalize_context = agent._build_consultant_finalize_context(
            task_id="session_1786266235",
            task_spec=task_state["task_spec"],
            consultation_state=consultation,
            resolved_selected_skill_ids=selected_skills,
            input_audit=task_state["input_audit"],
        )

        coverage = classify_directive_coverage(finalize_context)
        compiled = compile_deterministic_coverage(finalize_context, coverage)
        self.assertEqual(coverage["directive_count"], 57)
        self.assertEqual(len(finalize_context["coverage_gaps"]), 44)
        self.assertEqual(coverage["counts"]["model_resolution_required"], 0)
        self.assertEqual(compiled["compile_mode"], "controller_deterministic")
        self.assertEqual(compiled["provider_status"], "not_called")
        self.assertEqual(compiled["next_step"], "planner")

    def test_heading_alias_and_duplicate_are_explicit_dispositions(self):
        context = {
            "finalize_context_id": "finalize:dedup",
            "base_contract_hash": "base:dedup",
            "resolved_selected_skill_ids": [SKILL_ID],
            "semantic_provenance_catalog": [
                {"source_id": "msg-a", "source_type": "user_explicit"},
                {"source_id": "msg-b", "source_type": "user_explicit"},
            ],
            "deterministic_base_constraints": [],
            "coverage_gaps": [],
            "unresolved_semantics": [],
            "effective_task_contract": {
                "required_inputs": [],
                "required_outputs": [{
                    "output_id": "report",
                    "filename": "report.md",
                    "source_type": "user_explicit",
                    "source_id": "msg-a",
                }],
                "parameters": [],
                "decisions": [],
                "source_addressable_directives": [
                    {
                        "directive_id": "heading",
                        "directive_kind": "required_behavior",
                        "normalized_text": "预期产物：",
                        "normalized_value": "预期产物：",
                        "semantic_key": "required_behavior.user_directive_0001",
                        "operator": "require",
                        "strength": "hard",
                        "source_ids": ["msg-a"],
                    },
                    {
                        "directive_id": "alias",
                        "directive_kind": "required_behavior",
                        "normalized_text": "report.md",
                        "normalized_value": "report.md",
                        "semantic_key": "required_behavior.user_directive_0002",
                        "operator": "require",
                        "strength": "hard",
                        "source_ids": ["msg-a"],
                    },
                    {
                        "directive_id": "duplicate-a",
                        "directive_kind": "forbidden_behavior",
                        "normalized_text": "Never fabricate output",
                        "normalized_value": "Never fabricate output",
                        "semantic_key": "forbidden_behavior.user_directive_0003",
                        "operator": "forbid",
                        "strength": "required",
                        "source_ids": ["msg-a"],
                    },
                    {
                        "directive_id": "duplicate-b",
                        "directive_kind": "forbidden_behavior",
                        "normalized_text": "Never fabricate output",
                        "normalized_value": "Never fabricate output",
                        "semantic_key": "forbidden_behavior.user_directive_0004",
                        "operator": "forbid",
                        "strength": "hard",
                        "source_ids": ["msg-b"],
                    },
                ],
            },
        }
        coverage = classify_directive_coverage(context)
        dispositions = {
            item["directive_id"]: item["disposition"]
            for item in coverage["entries"]
        }
        self.assertEqual(dispositions["heading"], "structural_only")
        self.assertEqual(dispositions["alias"], "typed_fact_alias")
        self.assertEqual(dispositions["duplicate-a"], "deterministic_constraint")
        self.assertEqual(dispositions["duplicate-b"], "duplicate_of")
        self.assertEqual(len(coverage["deterministic_constraints"]), 1)
        row = coverage["deterministic_constraints"][0]
        self.assertEqual(row["strength"], "hard")
        self.assertEqual(
            {item["source_id"] for item in row["provenance"]},
            {"msg-a", "msg-b"},
        )

    def test_synthetic_key_cannot_bypass_explicit_model_resolution(self):
        context = _model_context()
        coverage = classify_directive_coverage(context)
        self.assertEqual(coverage["counts"]["model_resolution_required"], 1)
        with self.assertRaisesRegex(ValueError, "model resolutions remain"):
            compile_deterministic_coverage(context, coverage)


class ConsultantFinalizeV4BatchTests(unittest.TestCase):
    def test_bounded_schema_strict_decode_and_pair_validation(self):
        context = _model_context(item_count=2)
        coverage, catalog = _catalog_for_context(context)
        batch = build_finalize_decision_batch(catalog)
        schema_artifact = build_finalize_batch_schema(batch, token_estimator=len, tokenizer_id="char-test", tokenizer_version="1")
        schema = schema_artifact["response_schema"]["oneOf"][0]
        resolutions_schema = schema["properties"]["resolutions"]
        self.assertEqual(resolutions_schema["minItems"], 2)
        self.assertEqual(resolutions_schema["maxItems"], 2)
        self.assertFalse(resolutions_schema["items"])
        self.assertGreater(schema_artifact["maximum_canonical_output_tokens"], 0)

        payload = _valid_payload(batch)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        result = validate_finalize_batch_response(
            raw,
            decision_batch=batch,
            candidate_catalog=catalog,
            response_schema_artifact=schema_artifact,
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["protocol_status"], "valid")

        duplicate_key_raw = raw.replace(
            '{"action_type":"resolve_batch"',
            '{"action_type":"resolve_batch","action_type":"resolve_batch"',
            1,
        )
        self.assertFalse(validate_finalize_batch_response(
            duplicate_key_raw,
            decision_batch=batch,
            candidate_catalog=catalog,
            response_schema_artifact=schema_artifact,
        )["valid"])
        with self.assertRaises(ValueError):
            strict_decode_finalize_batch_response('{"x":NaN}')
        with self.assertRaises(ValueError):
            strict_decode_finalize_batch_response('{"x":"\\ud800"}')
        with self.assertRaises(json.JSONDecodeError):
            strict_decode_finalize_batch_response(raw + " trailing")

        invalid_pair = deepcopy(payload)
        invalid_pair["resolutions"][0]["candidate_handle"] = payload["resolutions"][1]["candidate_handle"]
        rejected = validate_finalize_batch_response(
            json.dumps(invalid_pair, separators=(",", ":")),
            decision_batch=batch,
            candidate_catalog=catalog,
            response_schema_artifact=schema_artifact,
        )
        self.assertFalse(rejected["valid"])
        self.assertEqual(rejected["protocol_status"], "field_invalid")

        messages = build_finalize_batch_messages(batch, schema_artifact)
        self.assertEqual([message.type for message in messages], ["system", "human"])
        self.assertEqual(messages[0].content.count("<sole_response_schema"), 1)
        self.assertNotIn("deterministic_base_constraints", messages[1].content)
        self.assertNotIn("finalize_context_id", messages[0].content + messages[1].content)
        self.assertNotIn("_hash", messages[0].content + messages[1].content)

    def test_stable_capacity_partition_and_atomic_merge(self):
        context = _model_context(item_count=4)
        coverage, catalog = _catalog_for_context(context)
        ordered_ids = sorted(item["resolution_id"] for item in catalog["items"])
        probe_one = build_finalize_decision_batch(catalog, ordered_ids[:1])
        probe_schema = build_finalize_batch_schema(
            probe_one,
            token_estimator=len,
            tokenizer_id="char-test",
            tokenizer_version="1",
        )
        safety = 32
        one_item_cap = probe_schema["maximum_canonical_output_tokens"] + safety
        basis = build_partition_capacity_basis(
            catalog,
            provider_capability={
                "provider_id": "deepseek",
                "output_token_parameter": "max_tokens",
                "effective_token_cap_verified": True,
                "supports_bounded_output_grammar": False,
            },
            requested_output_tokens=one_item_cap,
            provider_output_cap=one_item_cap,
            provider_context_limit=100000,
            safety_headroom_tokens=safety,
            max_batch_items=4,
            tokenizer_id="char-test",
            tokenizer_version="1",
        )
        first = stable_partition_finalize_batches(
            catalog,
            basis,
            token_estimator=len,
        )
        second = stable_partition_finalize_batches(
            catalog,
            basis,
            token_estimator=len,
        )
        self.assertEqual(first["partition_hash"], second["partition_hash"])
        self.assertEqual(first["batch_hashes"], second["batch_hashes"])
        self.assertEqual(first["strategy"], "multi_batch")
        self.assertTrue(all(
            row["capacity_profile"]["canonical_admissible"]
            for row in first["batches"]
        ))
        self.assertTrue(all(
            row["capacity_profile"]["raw_completion_guaranteed"] is False
            for row in first["batches"]
        ))

        decisions = []
        for row in first["batches"]:
            batch = row["batch"]
            result = validate_finalize_batch_response(
                json.dumps(_valid_payload(batch), separators=(",", ":")),
                decision_batch=batch,
                candidate_catalog=catalog,
                response_schema_artifact=row["response_schema_artifact"],
            )
            self.assertTrue(result["valid"])
            decisions.append(result)
        with self.assertRaisesRegex(ValueError, "FINALIZE_BATCH_INCOMPLETE"):
            merge_finalize_batch_decisions(
                context,
                coverage,
                catalog,
                decisions[:-1],
            )
        merged = merge_finalize_batch_decisions(
            context,
            coverage,
            catalog,
            decisions,
        )
        self.assertEqual(merged["compile_mode"], "model_batched")
        self.assertEqual(merged["next_step"], "planner")
        self.assertEqual(
            len(merged["compiled_contract"]["constraints"]),
            4,
        )

    def test_unverified_wire_token_cap_blocks_partition(self):
        context = _model_context()
        _, catalog = _catalog_for_context(context)
        basis = build_partition_capacity_basis(
            catalog,
            provider_capability={
                "provider_id": "deepseek",
                "output_token_parameter": "max_tokens",
                "effective_token_cap_verified": False,
                "supports_bounded_output_grammar": False,
            },
            requested_output_tokens=4096,
            provider_output_cap=4096,
            provider_context_limit=200000,
            safety_headroom_tokens=128,
            max_batch_items=8,
        )
        with self.assertRaisesRegex(ValueError, "FINALIZE_PROVIDER_PARAMETER_UNSUPPORTED"):
            stable_partition_finalize_batches(catalog, basis)

    def test_work_preflight_proves_recovery_envelope_before_io(self):
        context = _model_context(item_count=2)
        _, catalog = _catalog_for_context(context)
        basis = build_partition_capacity_basis(
            catalog,
            provider_capability={
                "provider_id": "deepseek",
                "output_token_parameter": "max_tokens",
                "effective_token_cap_verified": True,
                "supports_bounded_output_grammar": False,
            },
            requested_output_tokens=100_000,
            provider_output_cap=100_000,
            provider_context_limit=200_000,
            safety_headroom_tokens=2_048,
            max_batch_items=64,
        )
        partition = stable_partition_finalize_batches(catalog, basis)
        profile = partition["batches"][0]["capacity_profile"]
        self.assertEqual(profile["available_output_tokens"], 100_000)
        self.assertEqual(
            profile["effective_output_tokens"],
            profile["maximum_canonical_output_tokens"] + 2_048,
        )

        admitted = build_finalize_work_preflight(
            partition,
            catalog,
            basis,
            max_resolution_items=1_024,
            max_created_batches=128,
            max_provider_calls=128,
            max_repartition_depth=8,
            max_total_completion_tokens=200_000,
            max_total_estimated_cost_microusd=50_000_000,
            estimated_cost_microusd_per_token=1,
        )
        self.assertTrue(admitted["admitted"])
        self.assertGreaterEqual(
            admitted["componentwise_worst_case"]["provider_calls"], 4
        )
        self.assertFalse(admitted["exceeded_fields"])

        rejected = build_finalize_work_preflight(
            partition,
            catalog,
            basis,
            max_resolution_items=1_024,
            max_created_batches=128,
            max_provider_calls=1,
            max_repartition_depth=8,
            max_total_completion_tokens=200_000,
            max_total_estimated_cost_microusd=50_000_000,
            estimated_cost_microusd_per_token=1,
        )
        self.assertFalse(rejected["admitted"])
        self.assertIn("provider_calls", rejected["exceeded_fields"])

    def test_default_200k_budget_admits_eight_items_and_blocks_sixteen(self):
        capability = {
            "provider_id": "deepseek",
            "output_token_parameter": "max_tokens",
            "effective_token_cap_verified": True,
            "supports_bounded_output_grammar": False,
        }
        policy = {
            "budget_policy_id": "finalize-work-budget-v1",
            "max_resolution_items": 1_024,
            "max_created_batches": 128,
            "max_provider_calls": 128,
            "max_repartition_depth": 8,
            "max_total_completion_tokens": 200_000,
            "max_total_estimated_cost_microusd": 50_000_000,
            "max_wall_time_ms": 900_000,
            "per_call_timeout_ms": 120_000,
            "estimated_cost_microusd_per_token": 1,
        }
        results = {}
        for item_count in (8, 16):
            context = _model_context(item_count=item_count)
            _, catalog = _catalog_for_context(context)
            basis = build_partition_capacity_basis(
                catalog,
                provider_capability=capability,
                requested_output_tokens=100_000,
                provider_output_cap=100_000,
                provider_context_limit=200_000,
                safety_headroom_tokens=2_048,
                max_batch_items=64,
                budget_policy=policy,
            )
            partition = stable_partition_finalize_batches(catalog, basis)
            results[item_count] = build_finalize_work_preflight(
                partition,
                catalog,
                basis,
                max_resolution_items=policy["max_resolution_items"],
                max_created_batches=policy["max_created_batches"],
                max_provider_calls=policy["max_provider_calls"],
                max_repartition_depth=policy["max_repartition_depth"],
                max_total_completion_tokens=policy[
                    "max_total_completion_tokens"
                ],
                max_total_estimated_cost_microusd=policy[
                    "max_total_estimated_cost_microusd"
                ],
                estimated_cost_microusd_per_token=policy[
                    "estimated_cost_microusd_per_token"
                ],
            )

        self.assertTrue(results[8]["admitted"])
        self.assertEqual(
            results[8]["componentwise_worst_case"]["completion_tokens"],
            169_436,
        )
        self.assertFalse(results[16]["admitted"])
        self.assertEqual(
            results[16]["componentwise_worst_case"]["completion_tokens"],
            348_150,
        )
        self.assertEqual(results[16]["exceeded_fields"], ["completion_tokens"])


if __name__ == "__main__":
    unittest.main()
