import importlib.util
from pathlib import Path

import pytest
import yaml


def load_validator():
    repo_root = Path(__file__).resolve().parents[1]
    validator_path = (
        repo_root
        / "benchmarks"
        / "biomind_agent_benchmark"
        / "scripts"
        / "validate"
        / "validate_benchmark.py"
    )
    spec = importlib.util.spec_from_file_location("biomind_benchmark_validator", validator_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_biomind_benchmark_dataset_is_valid():
    validator = load_validator()
    errors, summary = validator.validate_benchmark()

    assert errors == []
    assert summary["open_tasks"] >= 9
    assert summary["mcq_questions"] >= 19
    assert summary["skills"] >= 41


def test_professional_fixture_manifest_points_to_msprime_data():
    repo_root = Path(__file__).resolve().parents[1]
    benchmark_dir = repo_root / "benchmarks" / "biomind_agent_benchmark"
    manifest_path = benchmark_dir / "manifests" / "professional_fixtures_manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    assert "msprime" in manifest["simulators"]
    assert "splatter" not in manifest["simulators"]
    assert "AlphaSimR" not in manifest["simulators"]

    required_files = {
        "fixtures_professional/msprime/genotype_matrix.tsv",
        "fixtures_professional/msprime/phenotype.tsv",
        "fixtures_professional/msprime/gwas_summary.tsv",
        "fixtures_professional/msprime/marker_annotation.tsv",
        "fixtures_professional/msprime/msprime_variants.vcf",
        "fixtures_professional/msprime/simulation_truth.tsv",
    }
    for relative_path in required_files:
        path = benchmark_dir / relative_path
        assert path.exists(), relative_path
        assert path.stat().st_size > 0, relative_path


def test_public_demo_fixtures_are_valid_when_present():
    repo_root = Path(__file__).resolve().parents[1]
    public_manifest = repo_root / "benchmarks" / "biomind_agent_benchmark" / "manifests" / "public_fixtures_manifest.yaml"
    if not public_manifest.exists():
        return
    if importlib.util.find_spec("cooler") is None or importlib.util.find_spec("scipy") is None:
        pytest.skip("public demo fixture validation requires cooler and scipy")

    validator_path = (
        repo_root
        / "benchmarks"
        / "biomind_agent_benchmark"
        / "scripts"
        / "validate"
        / "validate_public_demo_fixtures.py"
    )
    spec = importlib.util.spec_from_file_location("biomind_public_fixture_validator", validator_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.validate_public_demo_fixtures() == []
