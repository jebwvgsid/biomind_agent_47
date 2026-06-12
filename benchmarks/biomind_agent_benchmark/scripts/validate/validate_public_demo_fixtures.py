from __future__ import annotations

import hashlib
from pathlib import Path

import cooler
import yaml
from scipy import io


BENCHMARK_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BENCHMARK_DIR.parents[1]
PUBLIC_DIR = BENCHMARK_DIR / "fixtures_public"
PUBLIC_MANIFEST = BENCHMARK_DIR / "manifests" / "public_fixtures_manifest.yaml"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def resolve_fixture_path(rel_path: str) -> Path:
    path = Path(rel_path)
    if path.is_absolute():
        return path
    repo_candidate = REPO_ROOT / path
    if repo_candidate.exists():
        return repo_candidate
    return BENCHMARK_DIR / path


def manifest_file_path(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("path") or "")
    return str(entry)


def manifest_file_availability(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("availability") or "available")
    return "available"


def validate_pbmc3k() -> list[str]:
    errors: list[str] = []
    root = PUBLIC_DIR / "pbmc3k_10x_subset"
    answer = load_yaml(root / "answer_key.yaml")["expected"]
    matrix = io.mmread(root / "matrix.mtx").tocsr()
    features = (root / "features.tsv").read_text(encoding="utf-8").splitlines()
    barcodes = (root / "barcodes.tsv").read_text(encoding="utf-8").splitlines()
    if list(matrix.shape) != answer["matrix_shape_genes_by_cells"]:
        errors.append(f"PBMC3k matrix shape mismatch: {matrix.shape}")
    if len(features) != answer["matrix_shape_genes_by_cells"][0]:
        errors.append("PBMC3k features.tsv row count mismatch")
    if len(barcodes) != answer["matrix_shape_genes_by_cells"][1]:
        errors.append("PBMC3k barcodes.tsv row count mismatch")
    if int(matrix.nnz) != int(answer["nonzero_entries"]):
        errors.append("PBMC3k nonzero entry count mismatch")
    if int(matrix.sum()) != int(answer["total_counts"]):
        errors.append("PBMC3k total count mismatch")
    return errors


def validate_chrm() -> list[str]:
    errors: list[str] = []
    root = PUBLIC_DIR / "hg38_chrM"
    answer = load_yaml(root / "answer_key.yaml")["expected"]
    seq = ""
    header = ""
    for line in (root / "hg38_chrM.fa").read_text(encoding="utf-8").splitlines():
        if line.startswith(">"):
            header = line[1:].strip()
        else:
            seq += line.strip().upper()
    if header != answer["fasta_header"]:
        errors.append("hg38 chrM FASTA header mismatch")
    if len(seq) != int(answer["sequence_length"]):
        errors.append("hg38 chrM sequence length mismatch")
    if hashlib.md5(seq.encode("ascii")).hexdigest() != answer["md5_sequence_uppercase"]:
        errors.append("hg38 chrM sequence md5 mismatch")
    return errors


def validate_cool() -> list[str]:
    errors: list[str] = []
    root = PUBLIC_DIR / "cool_hg19_gm12878_chr22"
    answer = load_yaml(root / "answer_key.yaml")["expected"]
    clr = cooler.Cooler(str(root / "hg19_GM12878_MboI_chr22_2Mb.cool"))
    pixels = clr.pixels()[:]
    if list(clr.chromnames) != answer["chromosomes"]:
        errors.append("cool chromosome list mismatch")
    if int(clr.binsize) != int(answer["bin_size"]):
        errors.append("cool bin size mismatch")
    if int(clr.info["nbins"]) != int(answer["nbins"]):
        errors.append("cool nbins mismatch")
    if int(clr.info["nnz"]) != int(answer["nnz"]):
        errors.append("cool nnz mismatch")
    if int(pixels["count"].sum()) != int(answer["sum_counts"]):
        errors.append("cool sum_counts mismatch")
    return errors


def validate_public_demo_fixtures() -> list[str]:
    errors: list[str] = []
    if not PUBLIC_MANIFEST.exists():
        return [f"Missing {PUBLIC_MANIFEST}"]
    for dataset in load_yaml(PUBLIC_MANIFEST).get("datasets", []):
        for item in dataset.get("files", []):
            if manifest_file_availability(item) == "planned":
                continue
            relative = manifest_file_path(item)
            path = resolve_fixture_path(str(relative))
            if not path.exists():
                errors.append(f"Missing public fixture: {relative}")
            elif path.stat().st_size == 0:
                errors.append(f"Empty public fixture: {relative}")
    errors.extend(validate_pbmc3k())
    errors.extend(validate_chrm())
    errors.extend(validate_cool())
    return errors


def main() -> int:
    errors = validate_public_demo_fixtures()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Public demo fixture validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
