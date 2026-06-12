from __future__ import annotations

import argparse
import gzip
import hashlib
import time
import shutil
import tarfile
import urllib.request
from pathlib import Path

import cooler
import numpy as np
import pandas as pd
import yaml
from scipy import io, sparse


BENCHMARK_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BENCHMARK_DIR.parents[1]
DEFAULT_OUTDIR = BENCHMARK_DIR / "fixtures_public"
DEFAULT_RAWDIR = REPO_ROOT / ".biomind" / "public_raw"

PBMC3K_URL = (
    "https://cf.10xgenomics.com/samples/cell-exp/1.1.0/pbmc3k/"
    "pbmc3k_filtered_gene_bc_matrices.tar.gz"
)
HG38_CHRM_URL = "https://hgdownload.soe.ucsc.edu/goldenPath/hg38/chromosomes/chrM.fa.gz"
COOLER_EXAMPLE_ARCHIVE = Path("/usr/share/doc/python3-cooler/tests.tar.xz")
COOLER_EXAMPLE_MEMBER = "tests/data/hg19.GM12878-MboI.matrix.2000kb.cool"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, path: Path) -> Path:
    ensure_dir(path.parent)
    if path.exists() and path.stat().st_size > 0:
        return path
    tmp = path.with_suffix(path.suffix + ".tmp")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 BioMind benchmark fixture downloader",
            "Accept": "*/*",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=120) as response, tmp.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            break
        except Exception as exc:  # pragma: no cover - network-dependent branch
            last_error = exc
            if tmp.exists():
                tmp.unlink()
            if attempt == 3:
                raise
            time.sleep(2 * attempt)
    if last_error is not None and not tmp.exists():
        raise last_error
    tmp.replace(path)
    return path


def write_yaml(path: Path, payload: dict) -> None:
    ensure_dir(path.parent)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    ensure_dir(path.parent)
    pd.DataFrame(rows).to_csv(path, sep="\t", index=False)


def rel_from_benchmark_or_repo(path: Path) -> str:
    target = path.resolve()
    for base in (BENCHMARK_DIR, REPO_ROOT):
        try:
            return target.relative_to(base).as_posix()
        except ValueError:
            continue
    return target.as_posix()


def subset_pbmc3k(rawdir: Path, outdir: Path) -> dict:
    archive = download(PBMC3K_URL, rawdir / "pbmc3k_filtered_gene_bc_matrices.tar.gz")
    extract_root = ensure_dir(rawdir / "pbmc3k_filtered_gene_bc_matrices")
    marker = extract_root / ".extracted"
    if not marker.exists():
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(extract_root)
        marker.write_text("ok\n", encoding="utf-8")

    matrix_dir = extract_root / "filtered_gene_bc_matrices" / "hg19"
    matrix = io.mmread(matrix_dir / "matrix.mtx").tocsr()
    genes = pd.read_csv(matrix_dir / "genes.tsv", sep="\t", header=None, names=["gene_id", "gene_name"])
    barcodes = pd.read_csv(matrix_dir / "barcodes.tsv", sep="\t", header=None, names=["barcode"])

    cell_counts = np.asarray(matrix.sum(axis=0)).ravel()
    gene_counts = np.asarray(matrix.sum(axis=1)).ravel()
    selected_cells = np.argsort(-cell_counts)[:100]
    selected_genes = np.argsort(-gene_counts)[:500]
    selected_cells.sort()
    selected_genes.sort()
    subset = matrix[selected_genes, :][:, selected_cells].astype(np.int32).tocoo()

    out = ensure_dir(outdir / "pbmc3k_10x_subset")
    io.mmwrite(out / "matrix.mtx", subset)
    genes_out = genes.iloc[selected_genes].copy()
    genes_out["feature_type"] = "Gene Expression"
    genes_out.to_csv(out / "features.tsv", sep="\t", index=False, header=False)
    barcodes.iloc[selected_cells].to_csv(out / "barcodes.tsv", sep="\t", index=False, header=False)

    subset_csr = subset.tocsr()
    total_by_cell = np.asarray(subset_csr.sum(axis=0)).ravel()
    detected_by_cell = np.asarray((subset_csr > 0).sum(axis=0)).ravel()
    total_by_gene = np.asarray(subset_csr.sum(axis=1)).ravel()
    mt_mask = genes_out["gene_name"].str.startswith("MT-", na=False).to_numpy()
    mt_counts = np.asarray(subset_csr[mt_mask, :].sum(axis=0)).ravel() if mt_mask.any() else np.zeros(100)
    metadata = pd.DataFrame(
        {
            "barcode": barcodes.iloc[selected_cells]["barcode"].to_list(),
            "total_counts": total_by_cell.astype(int),
            "detected_genes": detected_by_cell.astype(int),
            "mitochondrial_counts": mt_counts.astype(int),
            "mitochondrial_fraction": np.round(mt_counts / np.maximum(total_by_cell, 1), 6),
        }
    )
    metadata.to_csv(out / "cell_qc_metrics.tsv", sep="\t", index=False)

    top_gene_idx = int(np.argmax(total_by_gene))
    answer = {
        "dataset_id": "public-pbmc3k-10x-subset",
        "source_url": PBMC3K_URL,
        "source_sha256": sha256_file(archive),
        "subset_rule": "top 100 cells by original library size and top 500 genes by original total counts, both sorted by original index",
        "expected": {
            "matrix_shape_genes_by_cells": [int(subset.shape[0]), int(subset.shape[1])],
            "nonzero_entries": int(subset.nnz),
            "total_counts": int(subset.data.sum()),
            "median_counts_per_cell": float(np.median(total_by_cell)),
            "median_detected_genes_per_cell": float(np.median(detected_by_cell)),
            "mitochondrial_gene_count": int(mt_mask.sum()),
            "top_gene_by_subset_counts": {
                "gene_id": str(genes_out.iloc[top_gene_idx]["gene_id"]),
                "gene_name": str(genes_out.iloc[top_gene_idx]["gene_name"]),
                "counts": int(total_by_gene[top_gene_idx]),
            },
        },
    }
    write_yaml(out / "answer_key.yaml", answer)
    return {
        "id": answer["dataset_id"],
        "source_url": PBMC3K_URL,
        "files": [rel_from_benchmark_or_repo(out / name) for name in [
            "matrix.mtx",
            "features.tsv",
            "barcodes.tsv",
            "cell_qc_metrics.tsv",
            "answer_key.yaml",
        ]],
    }


def subset_hg38_chrm(rawdir: Path, outdir: Path) -> dict:
    gz_path = download(HG38_CHRM_URL, rawdir / "hg38_chrM.fa.gz")
    out = ensure_dir(outdir / "hg38_chrM")
    fasta = out / "hg38_chrM.fa"
    with gzip.open(gz_path, "rt", encoding="utf-8") as src, fasta.open("w", encoding="utf-8") as dst:
        shutil.copyfileobj(src, dst)

    header = ""
    seq_parts: list[str] = []
    for line in fasta.read_text(encoding="utf-8").splitlines():
        if line.startswith(">"):
            header = line[1:].strip()
        else:
            seq_parts.append(line.strip().upper())
    sequence = "".join(seq_parts)
    gc = sum(base in {"G", "C"} for base in sequence)
    answer = {
        "dataset_id": "public-ucsc-hg38-chrM",
        "source_url": HG38_CHRM_URL,
        "source_sha256": sha256_file(gz_path),
        "subset_rule": "full hg38 chrM chromosome FASTA from UCSC chromosome download",
        "expected": {
            "fasta_header": header,
            "sequence_length": len(sequence),
            "gc_count": int(gc),
            "gc_fraction": round(gc / len(sequence), 6),
            "md5_sequence_uppercase": hashlib.md5(sequence.encode("ascii")).hexdigest(),
        },
    }
    write_yaml(out / "answer_key.yaml", answer)
    return {
        "id": answer["dataset_id"],
        "source_url": HG38_CHRM_URL,
        "files": [
            rel_from_benchmark_or_repo(out / "hg38_chrM.fa"),
            rel_from_benchmark_or_repo(out / "answer_key.yaml"),
        ],
    }


def extract_cooler_example(rawdir: Path) -> Path:
    if not COOLER_EXAMPLE_ARCHIVE.exists():
        raise FileNotFoundError(
            f"{COOLER_EXAMPLE_ARCHIVE} not found. Install python3-cooler-examples in WSL."
        )
    target = rawdir / "cooler_examples" / COOLER_EXAMPLE_MEMBER
    if target.exists():
        return target
    ensure_dir(target.parent)
    with tarfile.open(COOLER_EXAMPLE_ARCHIVE, "r") as tar:
        tar.extract(COOLER_EXAMPLE_MEMBER, rawdir / "cooler_examples")
    return target


def subset_cool_chr22(rawdir: Path, outdir: Path) -> dict:
    source = extract_cooler_example(rawdir)
    src = cooler.Cooler(str(source))
    chrom = "chr22"
    bins_all = src.bins()[:]
    bin_mask = bins_all["chrom"] == chrom
    old_ids = np.flatnonzero(bin_mask.to_numpy())
    if len(old_ids) == 0:
        raise ValueError(f"{chrom} not found in {source}")
    old_start = int(old_ids[0])
    old_end = int(old_ids[-1])
    bins = bins_all.iloc[old_ids][["chrom", "start", "end"]].reset_index(drop=True)

    pixels = src.pixels()[:]
    pix_mask = (
        (pixels["bin1_id"] >= old_start)
        & (pixels["bin1_id"] <= old_end)
        & (pixels["bin2_id"] >= old_start)
        & (pixels["bin2_id"] <= old_end)
    )
    pixels = pixels.loc[pix_mask].copy()
    pixels["bin1_id"] = pixels["bin1_id"] - old_start
    pixels["bin2_id"] = pixels["bin2_id"] - old_start
    pixels = pixels[["bin1_id", "bin2_id", "count"]].reset_index(drop=True)

    out = ensure_dir(outdir / "cool_hg19_gm12878_chr22")
    cool_path = out / "hg19_GM12878_MboI_chr22_2Mb.cool"
    if cool_path.exists():
        cool_path.unlink()
    cooler.create_cooler(str(cool_path), bins=bins, pixels=pixels, ordered=True)
    bins.to_csv(out / "bins.tsv", sep="\t", index=False)
    pixels.to_csv(out / "pixels.tsv", sep="\t", index=False)

    dst = cooler.Cooler(str(cool_path))
    answer = {
        "dataset_id": "public-cooler-hg19-gm12878-mboi-chr22",
        "source_package": "Ubuntu python3-cooler-examples 0.8.11-2",
        "source_member": COOLER_EXAMPLE_MEMBER,
        "source_sha256": sha256_file(source),
        "subset_rule": "single chromosome chr22 retained from hg19 GM12878 MboI 2Mb cooler test matrix",
        "expected": {
            "chromosomes": list(dst.chromnames),
            "bin_size": int(dst.binsize),
            "nbins": int(dst.info["nbins"]),
            "nnz": int(dst.info["nnz"]),
            "sum_counts": int(pixels["count"].sum()),
            "max_pixel_count": int(pixels["count"].max()),
        },
    }
    write_yaml(out / "answer_key.yaml", answer)
    return {
        "id": answer["dataset_id"],
        "source_url": "apt://python3-cooler-examples",
        "files": [rel_from_benchmark_or_repo(out / name) for name in [
            "hg19_GM12878_MboI_chr22_2Mb.cool",
            "bins.tsv",
            "pixels.tsv",
            "answer_key.yaml",
        ]],
    }


def write_manifest(outdir: Path, datasets: list[dict]) -> None:
    payload = {
        "fixture_set_id": "biomind-public-demo-fixtures",
        "version": "0.1.0",
        "root": rel_from_benchmark_or_repo(outdir),
        "notes": [
            "Public demo fixtures are downloaded or extracted from public package data, then subsetted for fast benchmark execution.",
            "Each dataset has answer_key.yaml with expected dimensions/checksums/summary statistics.",
            "Raw downloads are cached under .biomind/public_raw and are not required in git.",
        ],
        "datasets": datasets,
        "task_fixtures": {
            "BM-SCRNA-INGEST-QC-001": [
                rel_from_benchmark_or_repo(outdir / "pbmc3k_10x_subset" / "matrix.mtx"),
                rel_from_benchmark_or_repo(outdir / "pbmc3k_10x_subset" / "features.tsv"),
                rel_from_benchmark_or_repo(outdir / "pbmc3k_10x_subset" / "barcodes.tsv"),
                rel_from_benchmark_or_repo(outdir / "pbmc3k_10x_subset" / "cell_qc_metrics.tsv"),
                rel_from_benchmark_or_repo(outdir / "pbmc3k_10x_subset" / "answer_key.yaml"),
            ],
            "BM-SCRNA-FULL-002": [
                rel_from_benchmark_or_repo(outdir / "pbmc3k_10x_subset" / "matrix.mtx"),
                rel_from_benchmark_or_repo(outdir / "pbmc3k_10x_subset" / "cell_qc_metrics.tsv"),
                rel_from_benchmark_or_repo(outdir / "pbmc3k_10x_subset" / "answer_key.yaml"),
            ],
            "BM-3DDNA-TAD-003": [
                rel_from_benchmark_or_repo(outdir / "cool_hg19_gm12878_chr22" / "hg19_GM12878_MboI_chr22_2Mb.cool"),
                rel_from_benchmark_or_repo(outdir / "cool_hg19_gm12878_chr22" / "answer_key.yaml"),
            ],
            "BM-SEQ-PUBLIC-HOMOLOGY-009": [
                rel_from_benchmark_or_repo(outdir / "hg38_chrM" / "hg38_chrM.fa"),
                rel_from_benchmark_or_repo(outdir / "hg38_chrM" / "answer_key.yaml"),
            ],
        },
    }
    write_yaml(BENCHMARK_DIR / "manifests" / "public_fixtures_manifest.yaml", payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download and subset public demo fixtures for BioMind.")
    parser.add_argument("--outdir", default=str(DEFAULT_OUTDIR), help="Output fixture directory.")
    parser.add_argument("--rawdir", default=str(DEFAULT_RAWDIR), help="Raw download cache directory.")
    args = parser.parse_args()

    outdir = ensure_dir(Path(args.outdir).resolve())
    rawdir = ensure_dir(Path(args.rawdir).resolve())
    datasets = [
        subset_pbmc3k(rawdir, outdir),
        subset_hg38_chrm(rawdir, outdir),
        subset_cool_chr22(rawdir, outdir),
    ]
    write_manifest(outdir, datasets)
    print(f"Generated public demo fixtures under {outdir}")
    print(f"Manifest: {BENCHMARK_DIR / 'manifests' / 'public_fixtures_manifest.yaml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
