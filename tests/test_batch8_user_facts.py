import json
import unittest

from agent_core.task_spec import (
    apply_user_feedback,
    artifact_locator,
    create_task_spec,
    ensure_task_spec,
    merge_compiled_constraints,
    normalize_fact_candidates_with_skill,
    resolve_effective_user_facts,
    semantic_constraints_from_task_spec,
    validate_compiled_constraints,
)


class Batch8AUserFactTests(unittest.TestCase):
    def test_seven_turn_annotation_facts_are_structured(self):
        spec = create_task_spec("我要对植物 VCF 做自定义基因组注释。", task_id="task-a")
        turns = [
            "物种是花生 Arachis hypogaea。",
            "VCF 在 /tmp/test/all.filtered.snp.vcf。",
            "参考基因组是 /tmp/test/genome.fasta。",
            "注释文件是 /tmp/test/genome.gtf，必须使用 -gtf22。",
            "不要猜已有物种数据库。",
            "输出目录改为 /tmp/test/output/strict。",
        ]
        for index, text in enumerate(turns, 2):
            spec = apply_user_feedback(
                spec,
                text,
                kind="consultant_answer",
                source_message_id=f"msg-{index}",
                source_task_id="task-a",
                message_origin="user",
            )
        compiled = validate_compiled_constraints([
            {
                "constraint_id": "annotation-format",
                "semantic_key": "required_behavior.annotation_format",
                "operator": "eq",
                "value": "gtf22",
                "strength": "hard",
                "confidence": 0.99,
                "source_text": turns[3],
            },
            {
                "constraint_id": "forbid-species-db-guess",
                "semantic_key": "forbidden_behavior.species_database_guess",
                "operator": "forbid",
                "value": True,
                "strength": "hard",
                "confidence": 0.98,
                "source_text": turns[4],
            },
        ])
        self.assertTrue(compiled["valid"], compiled["errors"])
        spec = merge_compiled_constraints(spec, compiled["constraints"], metrics=compiled["metrics"])

        artifact_roles = {item["role"] for item in spec["inputs"]}
        entities = {
            (item["entity_type"], item["value"])
            for item in resolve_effective_user_facts(spec)["active_entities"]
        }
        parameters = spec["parameters"]
        constraints = {
            item["semantic_key"]: item["value"]
            for item in semantic_constraints_from_task_spec(spec)
        }
        outputs = spec["output_contracts"]

        self.assertTrue({"variant_vcf", "reference_fasta", "annotation_gtf"} <= artifact_roles)
        self.assertIn(("species", "花生 Arachis hypogaea"), entities)
        self.assertTrue(parameters["cli.gtf22"])
        self.assertEqual(constraints["required_behavior.annotation_format"], "gtf22")
        self.assertTrue(constraints["forbidden_behavior.species_database_guess"])
        self.assertEqual(artifact_locator(outputs[-1]), "/tmp/test/output/strict")
        self.assertGreaterEqual(len(spec["source_refs"]), len(turns))
        self.assertNotIn("facts", spec)
        self.assertNotIn("artifact_bindings", spec)

    def test_single_cell_facts_are_generic_bindings(self):
        spec = create_task_spec("做单细胞下游分析", task_id="scrna")
        for text in (
            "输入是 /data/seurat_qc.rds。",
            "marker reference 使用 /data/marker_reference.csv。",
            "clustering_resolution=0.6。",
            "上游缺失时必须 blocked_missing_upstream。",
        ):
            spec = apply_user_feedback(spec, text, kind="consultant_answer", message_origin="user")
        compiled = validate_compiled_constraints([{
            "constraint_id": "missing-upstream-policy",
            "semantic_key": "failure_policy.upstream_missing",
            "operator": "on_condition",
            "value": "blocked_missing_upstream",
            "strength": "conditional",
            "confidence": 0.96,
            "condition": "upstream_missing",
            "source_text": "上游缺失时必须 blocked_missing_upstream。",
        }])
        self.assertTrue(compiled["valid"], compiled["errors"])
        spec = merge_compiled_constraints(spec, compiled["constraints"], metrics=compiled["metrics"])

        roles = {item["role"] for item in spec["inputs"]}
        params = spec["parameters"]
        self.assertIn("seurat_qc_object", roles)
        self.assertIn("marker_reference", roles)
        self.assertEqual(params["clustering_resolution"], 0.6)
        policies = {
            item["semantic_key"]: item["value"]
            for item in semantic_constraints_from_task_spec(spec)
        }
        self.assertEqual(policies["failure_policy.upstream_missing"], "blocked_missing_upstream")

    def test_hic_many_artifacts_and_relation_are_preserved(self):
        spec = create_task_spec("比较 /data/leaf.mcool 和 /data/bud.mcool，resolution=40000，leaf compared_with bud", task_id="hic")

        contacts = [item for item in spec["inputs"] if item["role"] == "contact_matrix"]
        relations = resolve_effective_user_facts(spec)["active_relations"]
        self.assertEqual(len(contacts), 2)
        self.assertTrue(any(item["predicate"] == "compared_with" for item in relations))

    def test_gwas_and_enrichment_open_parameters(self):
        gwas = create_task_spec(
            "输入 /data/genotype.vcf、/data/phenotype.csv、/data/covariates.tsv，trait=trait_yield，model=MLM",
            task_id="gwas",
        )
        enrichment = create_task_spec(
            "使用 /data/gene_list.txt 和 /data/background_gene_list.txt，ontology=GO_BP，p_adjust_method=BH",
            task_id="enrichment",
        )

        gwas_roles = {item["role"] for item in gwas["inputs"]}
        gwas_params = gwas["parameters"]
        gwas_entities = {
            item["entity_type"]: item["value"]
            for item in resolve_effective_user_facts(gwas)["active_entities"]
        }
        enrichment_roles = {item["role"] for item in enrichment["inputs"]}
        enrichment_params = enrichment["parameters"]
        self.assertTrue({"genotype", "phenotype", "covariates"} <= gwas_roles)
        self.assertEqual(gwas_entities["trait"], "trait_yield")
        self.assertEqual(gwas_params["model"], "MLM")
        self.assertTrue({"gene_list", "background_gene_list"} <= enrichment_roles)
        self.assertEqual(enrichment_params["ontology"], "GO_BP")
        self.assertEqual(enrichment_params["p_adjust_method"], "BH")

    def test_no_file_task_does_not_invent_artifacts(self):
        spec = create_task_spec("请审查当前计划为什么循环，并解释错误。", task_id="review")
        self.assertEqual(spec["inputs"], [])
        self.assertEqual(spec["output_contracts"], [])

    def test_internal_human_messages_do_not_become_user_facts(self):
        spec = create_task_spec("解释任务", task_id="pollution")
        for origin in ("execute", "supervisor", "recovery", "rag", "controller", "runtime", "unknown"):
            updated = apply_user_feedback(
                spec,
                "发现 /internal/not-user.vcf，threads=999",
                kind="internal_observation",
                message_origin=origin,
            )
            self.assertEqual(updated, spec)

    def test_legacy_directive_migration_is_idempotent(self):
        legacy = {
            "schema_version": 3,
            "revision": 2,
            "goal": "做分析",
            "user_directives": [
                {"text": "做分析", "kind": "initial_request", "source": "user_initial"},
                {"text": "输入改为 /data/new.vcf", "kind": "consultant_answer", "source": "user", "source_message_id": "m2"},
            ],
        }
        once = ensure_task_spec(legacy, task_id="legacy")
        twice = ensure_task_spec(json.loads(json.dumps(once)), task_id="legacy")

        self.assertEqual(once, twice)
        binding = once["inputs"][0]
        self.assertEqual(artifact_locator(binding), "/data/new.vcf")
        self.assertEqual(binding["source_ref"], "m2")
        self.assertNotIn("artifact_bindings", once)
        self.assertNotIn("user_directives", once)

    def test_skill_aware_normalization_uses_contract_not_skill_id(self):
        candidates = [{
            "fact_kind": "artifact",
            "role": "table",
            "direction": "input",
            "locator": "/data/traits.csv",
            "artifact_type": "file",
            "format": "csv",
            "cardinality": "unknown",
            "required": True,
        }]
        contract = {
            "inputs": [{"role": "phenotype_table", "aliases": ["table"], "cardinality": "one", "required": True}],
            "parameters": {"model": {"type": "string"}},
        }

        normalized = normalize_fact_candidates_with_skill(candidates, contract)
        self.assertEqual(normalized[0]["role"], "phenotype_table")
        self.assertEqual(normalized[0]["cardinality"], "one")

    def test_replace_and_withdraw_update_fact_status(self):
        spec = create_task_spec("输入是 /data/a.vcf", task_id="replace")
        spec = apply_user_feedback(
            spec,
            "把 /data/a.vcf 换成 /data/c.vcf",
            kind="consultant_answer",
            source_task_id="replace",
        )
        self.assertEqual(
            [artifact_locator(item) for item in spec["inputs"]],
            ["/data/c.vcf"],
        )
        self.assertNotIn("artifact_bindings", spec)

        spec = apply_user_feedback(
            spec,
            "不再使用 /data/c.vcf",
            kind="consultant_answer",
            source_task_id="replace",
        )
        self.assertEqual(spec["inputs"], [])

    def test_retain_only_withdraws_unlisted_many_values(self):
        spec = create_task_spec(
            "比较 /data/a.mcool、/data/b.mcool、/data/c.mcool",
            task_id="retain",
        )
        spec = apply_user_feedback(
            spec,
            "只保留 /data/a.mcool 和 /data/b.mcool",
            kind="consultant_answer",
            source_task_id="retain",
        )
        active_paths = {
            artifact_locator(item) for item in spec["inputs"]
            if item["role"] == "contact_matrix"
        }
        self.assertEqual(active_paths, {"/data/a.mcool", "/data/b.mcool"})


if __name__ == "__main__":
    unittest.main()
