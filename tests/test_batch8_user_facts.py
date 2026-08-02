import json
import unittest

from agent_core.task_spec import (
    apply_user_feedback,
    create_task_spec,
    ensure_task_spec,
    normalize_fact_candidates_with_skill,
)


def active(spec, field):
    return [
        item for item in spec.get(field, [])
        if isinstance(item, dict) and item.get("status", "active") == "active"
    ]


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

        artifact_roles = {item["role"] for item in active(spec, "artifact_bindings")}
        entities = {(item["entity_type"], item["value"]) for item in active(spec, "entity_bindings")}
        parameters = {(item["namespace"], item["key"]): item["value"] for item in active(spec, "parameter_bindings")}
        requirements = {item["requirement_id"]: item["value"] for item in spec["requirements"]}
        constraints = "\n".join(item["text"] for item in active(spec, "constraint_bindings"))
        outputs = [item for item in active(spec, "artifact_bindings") if item["direction"] == "output"]

        self.assertTrue({"variant_vcf", "reference_fasta", "annotation_gtf"} <= artifact_roles)
        self.assertIn(("species", "花生 Arachis hypogaea"), entities)
        self.assertTrue(parameters[("cli", "gtf22")])
        self.assertEqual(requirements["annotation_format"], "gtf22")
        self.assertIn("不要猜已有物种数据库", constraints)
        self.assertEqual(outputs[-1]["locator"], "/tmp/test/output/strict")
        self.assertTrue(all(item.get("source_message_id") for item in active(spec, "facts")))

    def test_single_cell_facts_are_generic_bindings(self):
        spec = create_task_spec("做单细胞下游分析", task_id="scrna")
        for text in (
            "输入是 /data/seurat_qc.rds。",
            "marker reference 使用 /data/marker_reference.csv。",
            "clustering_resolution=0.6。",
            "上游缺失时必须 blocked_missing_upstream。",
        ):
            spec = apply_user_feedback(spec, text, kind="consultant_answer", message_origin="user")

        roles = {item["role"] for item in active(spec, "artifact_bindings")}
        params = {item["key"]: item["value"] for item in active(spec, "parameter_bindings")}
        self.assertIn("seurat_qc_object", roles)
        self.assertIn("marker_reference", roles)
        self.assertEqual(params["clustering_resolution"], 0.6)
        self.assertTrue(any("blocked_missing_upstream" in item["text"] for item in active(spec, "constraint_bindings")))

    def test_hic_many_artifacts_and_relation_are_preserved(self):
        spec = create_task_spec("比较 /data/leaf.mcool 和 /data/bud.mcool，resolution=40000，leaf compared_with bud", task_id="hic")

        contacts = [item for item in active(spec, "artifact_bindings") if item["role"] == "contact_matrix"]
        relations = active(spec, "relation_bindings")
        self.assertEqual(len(contacts), 2)
        self.assertEqual(contacts[0]["cardinality"], "many")
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

        gwas_roles = {item["role"] for item in active(gwas, "artifact_bindings")}
        gwas_params = {item["key"]: item["value"] for item in active(gwas, "parameter_bindings")}
        enrichment_roles = {item["role"] for item in active(enrichment, "artifact_bindings")}
        enrichment_params = {item["key"]: item["value"] for item in active(enrichment, "parameter_bindings")}
        self.assertTrue({"genotype", "phenotype", "covariates"} <= gwas_roles)
        self.assertEqual(gwas_params["trait"], "trait_yield")
        self.assertEqual(gwas_params["model"], "MLM")
        self.assertTrue({"gene_list", "background_gene_list"} <= enrichment_roles)
        self.assertEqual(enrichment_params["ontology"], "GO_BP")
        self.assertEqual(enrichment_params["p_adjust_method"], "BH")

    def test_no_file_task_does_not_invent_artifacts(self):
        spec = create_task_spec("请审查当前计划为什么循环，并解释错误。", task_id="review")
        self.assertEqual(active(spec, "artifact_bindings"), [])

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
        binding = active(once, "artifact_bindings")[0]
        self.assertEqual(binding["locator"], "/data/new.vcf")
        self.assertEqual(binding["migration_source"], "legacy_user_directive")

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
        artifacts = spec["artifact_bindings"]
        statuses = {item["locator"]: item["status"] for item in artifacts}
        self.assertEqual(statuses["/data/a.vcf"], "withdrawn")
        self.assertEqual(statuses["/data/c.vcf"], "active")

        spec = apply_user_feedback(
            spec,
            "不再使用 /data/c.vcf",
            kind="consultant_answer",
            source_task_id="replace",
        )
        self.assertFalse(any(
            item["locator"] == "/data/c.vcf" and item["status"] == "active"
            for item in spec["artifact_bindings"]
        ))

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
            item["locator"] for item in active(spec, "artifact_bindings")
            if item["role"] == "contact_matrix"
        }
        self.assertEqual(active_paths, {"/data/a.mcool", "/data/b.mcool"})


if __name__ == "__main__":
    unittest.main()
