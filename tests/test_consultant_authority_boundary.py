from __future__ import annotations

import tempfile
from pathlib import Path

from agent_core.consultation_state import (
    build_consultant_candidate,
    consultant_no_progress_decision,
    evaluate_consultant_ask,
    evaluate_consultant_candidate_route,
    new_consultation_state,
    seed_structured_evidence,
)
from agent_core.formal_artifact_resolver import resolve_formal_upstream_artifacts
from agent_core.skill_discovery import (
    new_skill_discovery_state,
    record_excluded_skills,
    record_loaded_skill_doc,
    record_loaded_skill_docs,
    record_observed_skill_mentions,
    record_selected_skills,
    record_skill_search_batch,
    selection_document_errors,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import create_task_spec, merge_task_spec_input_audit
from prompts.manager import PromptManager


ROOT = Path(__file__).resolve().parents[1]
SNPEFF = "genomics/snpeff-annotation"


def _empty_proposal() -> dict:
    return {
        "proposal_type": "task_spec_mutation",
        "schema_version": 1,
        "operations": [],
        "unresolved_ambiguities": [],
        "proposal_evidence_refs": [],
    }


def _write_skill(
    root: Path,
    skill_id: str,
    *,
    name: str,
    description: str,
    tags: list[str],
    body_marker: str,
) -> None:
    path = root / skill_id / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"tags: [{', '.join(tags)}]\n"
        "inputs: [variant_vcf]\n"
        "outputs: [report]\n"
        "---\n"
        f"# {name}\n\n{body_marker}\n",
        encoding="utf-8",
    )


def test_t01_catalog_is_internal_and_docs_are_not_automatically_loaded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill_root = Path(tmp)
        _write_skill(
            skill_root,
            "demo/alpha",
            name="Alpha",
            description="variant effect annotation",
            tags=["annotation"],
            body_marker="ALPHA_FULL_DOCUMENT_SENTINEL",
        )
        _write_skill(
            skill_root,
            "demo/beta",
            name="Beta",
            description="association analysis",
            tags=["gwas"],
            body_marker="BETA_FULL_DOCUMENT_SENTINEL",
        )
        manager = SkillManager(str(skill_root))
        cards = manager.search_skill_queries([
            {"query_id": "q1", "text": "variant annotation"}
        ], top_k=1, total_limit=1)["candidates"]
        discovery = new_skill_discovery_state()
        prompt = PromptManager.get_consultant_v2_prompt(
            {
                "evidence": [{"id": "U1", "text": "ambiguous request"}],
                "task": {},
                "feedback": {"consultation": {"skill_discovery": discovery}},
            },
            capability_context=manager.format_skill_discovery_cards(cards),
        )

    assert len(manager.discovered_skills) == 2
    assert cards[0]["skill_id"] == "demo/alpha"
    assert "demo/beta" not in prompt
    assert "ALPHA_FULL_DOCUMENT_SENTINEL" not in prompt
    assert "BETA_FULL_DOCUMENT_SENTINEL" not in prompt
    assert discovery["loaded_skill_ids"] == []


def test_t02_query_is_consultant_supplied_and_raw_prompt_is_not_selected() -> None:
    manager = SkillManager(str(ROOT / "skills"))
    raw_user_prompt = f"Maybe do something; observed ID {SNPEFF}"
    state = record_observed_skill_mentions(
        new_skill_discovery_state(),
        manager.observed_canonical_skill_mentions(raw_user_prompt),
    )
    consultant_query = "functional consequences for variants in an existing VCF"
    result = manager.search_skill_queries([
        {"query_id": "consultant-q1", "text": consultant_query}
    ], input_roles=["variant_vcf"], top_k=5)
    state = record_skill_search_batch(
        state,
        queries=result["queries"],
        candidate_cards=result["candidates"],
        catalog_digest=result["catalog_digest"],
    )

    assert result["queries"][0]["text"] == consultant_query
    assert raw_user_prompt not in state["search_queries_attempted"]
    assert state["selected_skill_refs"] == []
    assert state["observed_skill_mentions"] == [SNPEFF]


def test_t03_multi_hypothesis_search_is_stable_deduplicated_and_capped() -> None:
    manager = SkillManager(str(ROOT / "skills"))
    queries = [
        {"query_id": "q-gwas", "text": "GWAS mixed linear model population structure"},
        {"query_id": "q-emmax", "text": "EMMAX phenotype association Q matrix"},
        {"query_id": "q-report", "text": "GWAS Manhattan QQ plot report"},
    ]
    first = manager.search_skill_queries(queries, top_k=8, total_limit=7)
    second = manager.search_skill_queries(queries, top_k=8, total_limit=7)
    ids = [item["skill_id"] for item in first["candidates"]]

    assert len(first["queries"]) == 3
    assert len(ids) <= 7
    assert len(ids) == len(set(ids))
    assert ids == [item["skill_id"] for item in second["candidates"]]
    assert ids == [
        item["skill_id"]
        for item in sorted(
            first["candidates"],
            key=lambda row: (-row["deterministic_score"], row["skill_id"]),
        )
    ]


def test_t04_search_results_are_candidates_only_without_runtime_rows() -> None:
    manager = SkillManager(str(ROOT / "skills"))
    result = manager.search_skill_queries([
        {"query_id": "q1", "text": "GWAS EMMAX association"}
    ], top_k=5)
    discovery = record_skill_search_batch(
        new_skill_discovery_state(),
        queries=result["queries"],
        candidate_cards=result["candidates"],
        catalog_digest=result["catalog_digest"],
    )
    consultation = seed_structured_evidence(
        new_consultation_state("c04", "t04"),
        routing_candidate_ids=[],
        candidate_skill_ids=[],
        runtime_evidence={},
    )

    assert discovery["candidate_skill_refs"]
    assert discovery["selected_skill_refs"] == []
    assert not any(
        key.startswith("runtime:") for key in consultation["requirements"]
    )


def test_t05_raw_canonical_id_mention_is_observation_not_selection() -> None:
    manager = SkillManager(str(ROOT / "skills"))
    mentions = manager.observed_canonical_skill_mentions(
        f"Please consider {SNPEFF}, but decide the method."
    )
    discovery = record_observed_skill_mentions(
        new_skill_discovery_state(), mentions
    )
    consultation = seed_structured_evidence(
        new_consultation_state("c05", "t05"),
        routing_candidate_ids=[],
        candidate_skill_ids=[],
    )

    assert discovery["observed_skill_mentions"] == [SNPEFF]
    assert discovery["selected_skill_refs"] == []
    assert f"runtime:{SNPEFF}" not in consultation["requirements"]


def test_t06_direct_consultant_load_persists_digest_without_selection() -> None:
    manager = SkillManager(str(ROOT / "skills"))
    document = manager.load_skill(SNPEFF)
    meta = manager.get_skill_meta(SNPEFF)
    discovery = record_loaded_skill_docs(
        new_skill_discovery_state(),
        [{
            "skill_id": SNPEFF,
            "source_path": meta["_skill_md"],
            "document": document,
            "safe_summary": meta.get("description", ""),
        }],
        loaded_at_revision=2,
    )

    assert discovery["loaded_skill_ids"] == [SNPEFF]
    assert len(discovery["loaded_skill_docs"][SNPEFF]["document_digest"]) == 64
    assert discovery["selected_skill_refs"] == []

    with tempfile.TemporaryDirectory() as tmp:
        skill_root = Path(tmp)
        skill_id = "demo/digest-aware"
        _write_skill(
            skill_root,
            skill_id,
            name="Digest Aware",
            description="cache boundary fixture",
            tags=["fixture"],
            body_marker="FIRST_DOCUMENT_BODY",
        )
        cache_manager = SkillManager(str(skill_root))
        first_document = cache_manager.load_skill(skill_id)
        first_meta = cache_manager.get_skill_meta(skill_id)
        first_digest = first_meta["_skill_content_hash"]
        cached_state = record_loaded_skill_doc(
            new_skill_discovery_state(),
            skill_id=skill_id,
            path=first_meta["_skill_md"],
            document=first_document,
            document_digest=first_digest,
        )
        assert cached_state["pending_document_injection_ids"] == [skill_id]

        cached_state["pending_document_injection_ids"] = []
        cached_state["injected_document_digests"] = {skill_id: first_digest}
        repeated_document = cache_manager.load_skill(skill_id)
        cached_state = record_loaded_skill_doc(
            cached_state,
            skill_id=skill_id,
            path=first_meta["_skill_md"],
            document=repeated_document,
            document_digest=cache_manager.get_skill_meta(skill_id)[
                "_skill_content_hash"
            ],
        )
        assert repeated_document == first_document
        assert cached_state["pending_document_injection_ids"] == []

        _write_skill(
            skill_root,
            skill_id,
            name="Digest Aware",
            description="cache boundary fixture",
            tags=["fixture"],
            body_marker="SECOND_DOCUMENT_BODY_WITH_DIFFERENT_SIZE",
        )
        changed_document = cache_manager.load_skill(skill_id)
        changed_digest = cache_manager.get_skill_meta(skill_id)[
            "_skill_content_hash"
        ]
        cached_state = record_loaded_skill_doc(
            cached_state,
            skill_id=skill_id,
            path=first_meta["_skill_md"],
            document=changed_document,
            document_digest=changed_digest,
        )
        assert changed_digest != first_digest
        assert changed_document != first_document
        assert cached_state["pending_document_injection_ids"] == [skill_id]


def test_t07_load_before_select_returns_typed_consultant_error_then_clears() -> None:
    discovery = new_skill_discovery_state()
    errors = selection_document_errors(discovery, [SNPEFF])
    assert errors == [{
        "code": "SKILL_DOC_REQUIRED",
        "owner": "consultant",
        "field": f"selected_skill_refs[{SNPEFF}].document_digest",
        "skill_id": SNPEFF,
        "current_value": "",
        "expected_structural_condition": (
            "the selected Skill document is loaded at the current digest"
        ),
        "evidence": {"loaded_skill_ids": []},
        "allowed_action": "load_skill_doc",
    }]
    discovery = record_loaded_skill_doc(
        discovery, skill_id=SNPEFF, document="full immutable document"
    )
    assert selection_document_errors(discovery, [SNPEFF]) == []


def test_t08_selected_and_excluded_refs_are_typed_disjoint_consultant_state() -> None:
    emmax = "population-genetics/gwas-emmax-association"
    tassel = "population-genetics/gwas-tassel-glm-mlm"
    discovery = record_loaded_skill_docs(
        new_skill_discovery_state(),
        [
            {"skill_id": emmax, "document": "EMMAX full doc"},
            {"skill_id": tassel, "document": "TASSEL full doc"},
        ],
    )
    discovery = record_excluded_skills(discovery, [{
        "skill_id": tassel,
        "reason_code": "user_forbidden_substitute",
        "reason": "The user explicitly excludes TASSEL.",
    }], decision_revision=3)
    discovery = record_selected_skills(
        discovery,
        [emmax],
        decisions=[{
            "skill_id": emmax,
            "role": "primary",
            "reason": "Loaded document matches mixed-model GWAS.",
        }],
        selection_revision=3,
    )

    assert discovery["selected_skill_refs"][0]["skill_id"] == emmax
    assert discovery["selected_skill_refs"][0]["source"] == "consultant"
    assert discovery["excluded_skill_refs"][0]["skill_id"] == tassel
    assert discovery["excluded_skill_refs"][0]["source"] == "consultant"
    assert {emmax}.isdisjoint({tassel})


def test_t09_complete_selection_and_taskspec_routes_without_empty_finish_turn() -> None:
    document = "documented capability contract"
    discovery = record_loaded_skill_doc(
        new_skill_discovery_state(), skill_id=SNPEFF, document=document
    )
    discovery = record_selected_skills(
        discovery,
        [SNPEFF],
        decisions=[{
            "skill_id": SNPEFF,
            "role": "primary",
            "reason": "Documented VCF annotation compatibility.",
        }],
        selection_revision=1,
    )
    consultation = new_consultation_state("c09", "t09")
    consultation["skill_discovery"] = discovery
    candidate = build_consultant_candidate(
        create_task_spec("Annotate the supplied variants.", task_id="t09"),
        consultation,
        _empty_proposal(),
        selected_skills=[SNPEFF],
        task_id="t09",
        runtime_evidence={SNPEFF: {
            "evidence_status": "checked",
            "availability_status": "ready",
            "reason_code": "RUNTIME_READY",
        }},
    )

    assert evaluate_consultant_candidate_route(candidate)["route"] == "planner"
    assert candidate["consultation_state"]["completion_decision"] == (
        "ready_to_finalize"
    )


def test_t10_reformulated_empty_search_is_progress_but_identical_repeat_is_not() -> None:
    state1 = record_skill_search_batch(
        new_skill_discovery_state(),
        queries=[{"query_id": "q1", "text": "first vague hypothesis"}],
        candidate_cards=[],
        catalog_digest="catalog-a",
    )
    first = consultant_no_progress_decision(
        None,
        task_revision=1,
        conversation_turn_count=1,
        feedback=[{"code": "DISCOVERY_REQUIRED"}],
        route="consultant",
        discovery_state=state1,
    )
    prior = {**first["guard"], "retry_pending": True}
    state2 = record_skill_search_batch(
        state1,
        queries=[{"query_id": "q2", "text": "reformulated scientific method"}],
        candidate_cards=[],
        catalog_digest="catalog-a",
    )
    changed = consultant_no_progress_decision(
        prior,
        task_revision=1,
        conversation_turn_count=1,
        feedback=[{"code": "DISCOVERY_REQUIRED"}],
        route="consultant",
        discovery_state=state2,
    )
    repeated = record_skill_search_batch(
        state2,
        queries=[{"query_id": "q2", "text": "reformulated scientific method"}],
        candidate_cards=[],
        catalog_digest="catalog-a",
    )
    unchanged = consultant_no_progress_decision(
        {**changed["guard"], "retry_pending": True},
        task_revision=1,
        conversation_turn_count=1,
        feedback=[{"code": "DISCOVERY_REQUIRED"}],
        route="consultant",
        discovery_state=repeated,
    )

    assert changed["allow"] is True
    assert unchanged == {
        "allow": False,
        "code": "CONSULTANT_NO_PROGRESS",
        "guard": unchanged["guard"],
    }


def test_t11_genuine_scientific_ambiguity_is_user_owned_not_controller_choice() -> None:
    capability = {
        "required_bindings": [{
            "name": "scientific_analysis_goal",
            "required": True,
            "required_for_execution": True,
            "owner": "user",
            "user_input_required": True,
        }]
    }
    decision = evaluate_consultant_ask(
        {"requested_slots": ["scientific_analysis_goal"]},
        capabilities=[capability],
    )

    assert decision["allowed"] is True
    assert decision["code"] == "ASK_USER_SLOT_UNRESOLVED"


def test_t12_explicit_gff_without_required_fasta_does_not_scan_companions() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        gff = root / "Real.gff3"
        gff.write_text("##gff-version 3\n", encoding="utf-8")
        (root / "unrelated.fa").write_text(">chr1\nACGT\n", encoding="utf-8")
        audit = resolve_formal_upstream_artifacts(
            f"Use {gff}", work_dir=root
        )

    assert audit["candidate_groups"].get("reference_fasta") is None
    assert "reference_fasta" not in audit["ambiguous_roles"]
    assert "annotation_file" not in audit["ambiguous_roles"]
    assert any(item["path"] == str(gff.resolve()) for item in audit["resolved_inputs"])


def test_t13_required_fasta_search_returns_candidates_without_binding() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        gff = root / "Real.gff3"
        fasta = root / "genome.fa"
        gff.write_text("##gff-version 3\n", encoding="utf-8")
        fasta.write_text(">chr1\nACGT\n", encoding="utf-8")
        audit = resolve_formal_upstream_artifacts(
            f"Use {gff}",
            work_dir=root,
            candidate_search_roles=["reference_fasta"],
            required_input_roles=["reference_fasta"],
        )

    candidates = audit["candidate_groups"]["reference_fasta"]
    assert candidates[0]["path"] == str(fasta.resolve())
    assert not any(
        item["role"] == "reference_fasta" for item in audit["resolved_inputs"]
    )


def test_t14_explicit_binding_precedes_unconfirmed_discovered_candidates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        explicit = root / "Real.gff3"
        other = root / "Similar.gtf"
        explicit.write_text("##gff-version 3\n", encoding="utf-8")
        other.write_text("chr1\tx\tgene\t1\t2\t.\t+\t.\tgene_id \"g\";\n", encoding="utf-8")
        spec = create_task_spec("Bind the explicit annotation.", task_id="t14")
        merged = merge_task_spec_input_audit(spec, {
            "checked_at": "2026-09-01T00:00:00Z",
            "resolved_inputs": [
                {
                    "role": "annotation_file",
                    "path": str(explicit),
                    "source": "explicit",
                    "verified": True,
                },
                {
                    "role": "annotation_file",
                    "path": str(other),
                    "source": "local_candidate_search",
                    "verified": True,
                },
            ],
        })

    paths = {
        item.get("canonical_path") or item.get("path")
        for item in merged["inputs"]
    }
    assert str(explicit.resolve()) in paths
    assert str(other.resolve()) not in paths


def test_t23_snpeff_progressive_flow_reaches_planner_without_empty_turn() -> None:
    manager = SkillManager(str(ROOT / "skills"))
    search = manager.search_skill_queries([
        {"query_id": "snpeff", "text": "existing VCF functional effect annotation"}
    ], input_roles=["variant_vcf"], top_k=8)
    assert SNPEFF in {item["skill_id"] for item in search["candidates"]}
    discovery = record_skill_search_batch(
        new_skill_discovery_state(),
        queries=search["queries"],
        candidate_cards=search["candidates"],
        catalog_digest=search["catalog_digest"],
    )
    discovery = record_loaded_skill_doc(
        discovery,
        skill_id=SNPEFF,
        document=manager.load_skill(SNPEFF),
    )
    assert selection_document_errors(discovery, [SNPEFF]) == []
    discovery = record_selected_skills(discovery, [SNPEFF])
    consultation = new_consultation_state("c23", "t23")
    consultation["skill_discovery"] = discovery
    candidate = build_consultant_candidate(
        create_task_spec("Annotate variants in an existing VCF.", task_id="t23"),
        consultation,
        _empty_proposal(),
        selected_skills=[SNPEFF],
        task_id="t23",
        runtime_evidence={SNPEFF: {
            "evidence_status": "checked",
            "availability_status": "ready",
        }},
    )

    assert candidate["selected_skills"] == [SNPEFF]
    assert evaluate_consultant_candidate_route(candidate)["route"] == "planner"


def test_t24_prompt_payload_has_bounded_deltas_and_visible_runtime_rows() -> None:
    payload = {
        "evidence": [{"id": "U1", "text": "Run GWAS"}],
        "task": {"goal": "Run GWAS"},
        "feedback": {
            "consultation": {
                "skill_discovery": {
                    "search_history": [{
                        "queries": [{"query_id": "q1", "text": "GWAS LMM"}],
                        "candidate_ids": ["population-genetics/gwas-emmax-association"],
                    }],
                    "loaded_skill_refs": [{
                        "skill_id": "population-genetics/gwas-emmax-association",
                        "document_digest": "digest-emmax",
                    }],
                }
            },
            "runtime_checks": [{
                "skill_id": "population-genetics/gwas-emmax-association",
                "evidence_status": "checked",
                "availability_status": "unavailable",
                "reason_code": "CONFIGURED_ENV_NOT_AVAILABLE",
            }],
        },
        "telemetry": {"runtime_check_count": 1},
    }
    bounded_cards = (
        '{"skill_discovery_cards":[{"skill_id":'
        '"population-genetics/gwas-emmax-association"}]}'
    )
    prompt = PromptManager.get_consultant_v2_prompt(
        payload, capability_context=bounded_cards
    )

    assert "GWAS LMM" in prompt
    assert "digest-emmax" in prompt
    assert "CONFIGURED_ENV_NOT_AVAILABLE" in prompt
    assert '"runtime_check_count": 1' in prompt
    assert "FULL_SKILL_DOCUMENT_SENTINEL" not in prompt
    assert prompt.count("digest-emmax") == 1
