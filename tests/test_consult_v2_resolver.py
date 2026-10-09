"""V2 T13–T15: real resolver, synthetic files, explicit facts and scopes.

These tests replace obsolete assumptions that free text, BM identifiers or one
ranked candidate can establish a selected input. They do not run TAD analysis.
"""
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_core.formal_artifact_resolver import (
    RESOLUTION_POLICY_VERSION,
    _select_artifact_spec,
    formal_input_resolution_cache_key,
    format_formal_upstream_context,
    resolve_formal_upstream_artifacts,
)
from agent_core.skill_discovery import discovery_input_roles


def write(root, relative, body="chr1\t0\t40000\tdomain\n"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def bed(root, task="previous", sample="leaf"):
    return write(root, f"benchmarks/biomind_agent_benchmark/runs/{task}/TADs/40000/{sample}_domains.bed")


@pytest.mark.parametrize("task_id", ["session_1789876303", "BM-3DDNA-DIFFTAD-MEDIUM-008", "BM-3DDNA-BOUNDARY-STRICT-010"])
def test_t13_output_words_and_benchmark_id_do_not_create_input_need(task_id, monkeypatch):
    with TemporaryDirectory(prefix="consult-v2-resolver-") as directory:
        root = Path(directory)
        old = bed(root)
        matrix = write(root, "inputs/leaf.mcool", "synthetic matrix")
        output = root / "outputs"
        output.mkdir()
        def forbid_glob(*args, **kwargs):
            raise AssertionError("no input requirement exists: must not search recursively")
        monkeypatch.setattr(Path, "rglob", forbid_glob)
        result = resolve_formal_upstream_artifacts(
            f"Create TAD domains and boundaries from {matrix}, output {output}",
            root, task_id=task_id, project_root=root,
            known_inputs=[{"path": str(matrix), "role": "mcool_matrix"}],
            known_outputs=[{"path": str(output), "role": "output_dir"}],
        )
        assert result["artifact_role"] == ""
        assert result["formal_candidates"] == []
        assert result["search_roots"] == []
        assert result["selected_by_sample"] == {}
        assert {row["path"] for row in result["resolved_inputs"]} == {str(matrix)}
        assert "region_domain_bed" not in discovery_input_roles(input_audit=result)
        assert str(old) not in format_formal_upstream_context(result)
        assert _select_artifact_spec("TAD domains", task_id) is None


def test_t14_accepted_old_bed_is_verified_exactly_without_search(monkeypatch):
    with TemporaryDirectory(prefix="consult-v2-resolver-") as directory:
        root = Path(directory)
        selected = bed(root, "chosen")
        bed(root, "unrelated")
        def forbid_glob(*args, **kwargs):
            raise AssertionError("an accepted path must not trigger a replacement search")
        monkeypatch.setattr(Path, "rglob", forbid_glob)
        result = resolve_formal_upstream_artifacts(
            "Reuse accepted BED", root / "current", project_root=root,
            known_inputs=[{"path": str(selected), "role": "region_domain_bed", "source": "user_explicit"}],
            required_input_roles=["region_domain_bed"],
        )
        assert result["status"] == "resolved"
        assert result["verified_explicit_paths"] == [str(selected)]
        assert result["selected_by_sample"]["leaf"]["path"] == str(selected)
        assert result["formal_candidates"] == []
        assert discovery_input_roles(input_audit=result) == ["region_domain_bed"]


def test_t14_invalid_accepted_path_is_not_substituted(monkeypatch):
    with TemporaryDirectory(prefix="consult-v2-resolver-") as directory:
        root = Path(directory)
        alternative = bed(root)
        missing = root / "missing/leaf_domains.bed"
        def forbid_glob(*args, **kwargs):
            raise AssertionError("invalid exact input cannot silently fall back")
        monkeypatch.setattr(Path, "rglob", forbid_glob)
        result = resolve_formal_upstream_artifacts(
            "Use exactly this prior input", root / "current", project_root=root,
            known_inputs=[{"path": str(missing), "role": "region_domain_bed"}],
            required_input_roles=["region_domain_bed"],
            search_directories=[alternative.parent],
        )
        assert result["status"] == "missing_inputs"
        assert result["invalid_explicit_paths"] == [str(missing)]
        assert result["resolved_inputs"] == result["formal_candidates"] == []
        assert str(missing) in format_formal_upstream_context(result)
        assert str(alternative) not in format_formal_upstream_context(result)


@pytest.mark.parametrize("scope_key", ["search_directories", "associated_history_directories"])
def test_t14_scoped_search_and_linked_resume_return_candidates_without_selection(scope_key):
    with TemporaryDirectory(prefix="consult-v2-resolver-") as directory:
        root = Path(directory)
        expected = bed(root, "associated")
        unrelated = bed(root, "unrelated")
        result = resolve_formal_upstream_artifacts(
            "Input role was accepted by Consultant", root / "current", project_root=root,
            required_input_roles=["region_domain_bed"],
            **{scope_key: [expected.parent]},
        )
        assert result["search_roots"] == [str(expected.parent)]
        assert [row["path"] for row in result["formal_candidates"]] == [str(expected)]
        assert result["selected_by_sample"] == {}
        assert result["resolved_inputs"] == []
        assert result["status"] == "candidates_available"
        assert result["selection_required_roles"] == ["region_domain_bed"]
        assert discovery_input_roles(input_audit=result) == []
        assert str(unrelated) not in format_formal_upstream_context(result)


def test_t14_current_task_fallback_does_not_scan_project_runs():
    with TemporaryDirectory(prefix="consult-v2-resolver-") as directory:
        root = Path(directory)
        current = root / "current"
        expected = write(current, "leaf_domains.bed")
        bed(root, "unrelated")
        result = resolve_formal_upstream_artifacts(
            "accepted upstream requirement", current, project_root=root,
            candidate_search_roles=["region_domain_bed"],
        )
        assert result["search_roots"] == [str(current)]
        assert [row["path"] for row in result["formal_candidates"]] == [str(expected)]
        result = resolve_formal_upstream_artifacts(
            "accepted upstream requirement", root, project_root=root,
            candidate_search_roles=["region_domain_bed"],
        )
        assert result["search_roots"] == []
        assert result["formal_candidates"] == []


def test_t14_viewing_old_results_and_output_conflicts_preserves_direction():
    with TemporaryDirectory(prefix="consult-v2-resolver-") as directory:
        root = Path(directory)
        old = bed(root)
        viewed = resolve_formal_upstream_artifacts(f"View existing result {old}", root, project_root=root)
        assert viewed["status"] == "observed_paths"
        assert viewed["resolved_inputs"] == []
        assert viewed["observed_artifacts"][0]["exists"] is True
        assert viewed["observed_artifacts"][0]["direction"] == "unknown"
        output = write(root, "current/leaf_domains.bed")
        conflicts = resolve_formal_upstream_artifacts(
            f"Create domains at {output}", root / "current", project_root=root,
            candidate_search_roles=["region_domain_bed"],
            known_outputs=[{"path": str(output), "role": "region_domain_bed"}],
        )
        assert conflicts["formal_candidates"] == conflicts["resolved_inputs"] == []
        assert any(row["direction"] == "output" and row["exists"] for row in conflicts["observed_artifacts"])


def test_t15_cache_identity_invalidates_old_goals_roles_and_associations():
    with TemporaryDirectory(prefix="consult-v2-resolver-") as directory:
        root = Path(directory)
        old = bed(root)
        common = {"query": "current accepted goal", "work_dir": root, "project_root": root}
        baseline = formal_input_resolution_cache_key(**common)
        assert baseline == formal_input_resolution_cache_key(**common)
        variants = [
            {**common, "query": "updated user goal"},
            {**common, "known_inputs": [{"path": str(old), "role": "region_domain_bed"}]},
            {**common, "required_input_roles": ["region_domain_bed"]},
            {**common, "associated_history_directories": [old.parent]},
            {**common, "known_outputs": [{"path": str(old.parent)}]},
        ]
        assert all(formal_input_resolution_cache_key(**variant) != baseline for variant in variants)
        result = resolve_formal_upstream_artifacts(**common)
        assert result["request_fingerprint"] == baseline
        assert result["resolution_policy_version"] == RESOLUTION_POLICY_VERSION == 2
        assert "request_fingerprint" not in {"status": "resolved", "selected_by_sample": {"leaf": {"path": str(old)}}}


@pytest.mark.parametrize("candidate_metadata", [
    {"source": "local_candidate_search"},
    {"source": "candidate", "status": "resolved"},
    {"source_type": "candidate", "direction": "input", "status": "resolved"},
    {"status": "resolved"},
    {"status": "selected", "score": 99, "artifact_kind": "tad_domains"},
    {"binding_status": "candidate_only"},
])
def test_t15_prior_unaccepted_candidates_do_not_become_bound_inputs(candidate_metadata):
    with TemporaryDirectory(prefix="consult-v2-resolver-") as directory:
        root = Path(directory)
        old = bed(root)
        result = resolve_formal_upstream_artifacts(
            "new analysis", root, project_root=root,
            known_inputs=[{"path": str(old), "role": "region_domain_bed", **candidate_metadata}],
        )
        assert result["resolved_inputs"] == []
        assert result["selected_by_sample"] == {}
        accepted = resolve_formal_upstream_artifacts(
            "resume with previously accepted input", root, project_root=root,
            known_inputs=[{"path": str(old), "role": "region_domain_bed", "source": "user_explicit"}],
        )
        assert accepted["resolved_inputs"][0]["path"] == str(old)
        assert accepted["status"] == "resolved"


def test_generic_reference_search_respects_explicit_scope_and_never_promotes_unique_candidate():
    with TemporaryDirectory(prefix="consult-v2-resolver-") as directory:
        root = Path(directory)
        reference = write(root, "input/genome.fasta", ">chr1\nACGT\n")
        annotation = write(root, "declared/genome.gtf", "chr1\tanno\tgene\t1\t4\t.\t+\t.\tID=x\n")
        write(root, "unrelated/genome.gtf", "chr2\tanno\tgene\t1\t4\t.\t+\t.\tID=y\n")
        result = resolve_formal_upstream_artifacts(
            "accepted annotation requirement", root, project_root=root,
            known_inputs=[{"path": str(reference), "role": "reference_fasta"}],
            candidate_search_roles=["annotation_file"],
            required_input_roles=["annotation_file"],
            search_directories=[annotation.parent],
        )
        assert [row["path"] for row in result["candidate_groups"]["annotation_file"]] == [str(annotation)]
        assert [row["path"] for row in result["resolved_inputs"]] == [str(reference)]
        assert result["status"] == "candidates_available"


def test_unique_fastq_directory_group_is_candidate_but_accepted_mate_relationship_survives():
    with TemporaryDirectory(prefix="consult-v2-resolver-") as directory:
        root = Path(directory)
        first = write(root, "reads/sample_R1.fastq.gz")
        second = write(root, "reads/sample_R2.fastq.gz")
        result = resolve_formal_upstream_artifacts(
            "accepted read requirement", root, project_root=root,
            candidate_search_roles=["paired_fastq"], search_directories=[first.parent],
        )
        assert result["resolved_inputs"] == []
        assert len(result["candidate_groups"]["paired_fastq"]) == 1
        assert result["status"] == "candidates_available"
        accepted = resolve_formal_upstream_artifacts(
            "accepted reads", root, project_root=root,
            known_inputs=[{"path": str(first), "role": "fastq"}],
        )
        paired = [row for row in accepted["resolved_inputs"] if row["role"] == "paired_fastq"]
        assert paired[0]["paths"] == [str(first), str(second)]


def test_relative_explicit_path_uses_project_root_and_validates_declared_format():
    with TemporaryDirectory(prefix="consult-v2-resolver-") as directory:
        root = Path(directory)
        accepted = write(root, "input/leaf_domains.bed")
        write(root, "other/input/leaf_domains.bed", "different\n")
        result = resolve_formal_upstream_artifacts(
            "accepted relative path", root / "other", project_root=root,
            known_inputs=[{"path": "input/leaf_domains.bed", "role": "region_domain_bed"}],
        )
        assert result["verified_explicit_paths"] == [str(accepted)]
        wrong = write(root, "input/leaf.mcool")
        bad = resolve_formal_upstream_artifacts(
            "accepted role with incompatible path", root, project_root=root,
            known_inputs=[{"path": str(wrong), "role": "region_domain_bed"}],
        )
        assert bad["status"] == "missing_inputs"
        assert bad["invalid_explicit_paths"] == [str(wrong)]
        assert bad["resolved_inputs"] == []


def test_required_generic_role_remains_missing_and_accepted_state_is_not_mutated():
    from copy import deepcopy
    with TemporaryDirectory(prefix="consult-v2-resolver-") as directory:
        root = Path(directory)
        matrix = write(root, "inputs/leaf.mcool", "synthetic")
        state = {
            "inputs": [{"path": str(matrix), "role": "mcool_matrix", "source": "user_explicit"}],
            "execution_receipts": [{"status": "completed", "result": "retained"}],
            "committed_steps": ["prior_step"],
        }
        before = deepcopy(state)
        result = resolve_formal_upstream_artifacts(
            "accepted new input requirement", root, project_root=root,
            known_inputs=state["inputs"], required_input_roles=["sample_sheet"],
        )
        assert result["missing_roles"] == ["sample_sheet"]
        assert result["status"] == "missing_inputs"
        assert result["resolved_inputs"][0]["path"] == str(matrix)
        assert state == before


def test_explicit_bed_can_have_user_chosen_basename():
    with TemporaryDirectory(prefix="consult-v2-resolver-") as directory:
        root = Path(directory)
        regions = write(root, "selected/regions.bed")
        result = resolve_formal_upstream_artifacts(
            "accepted renamed domain input", root, project_root=root,
            known_inputs=[{"path": str(regions), "role": "region_domain_bed", "sample": "leaf"}],
        )
        assert result["status"] == "resolved"
        assert result["selected_by_sample"]["leaf"]["path"] == str(regions)
