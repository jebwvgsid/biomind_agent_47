#!/usr/bin/env python3
import argparse
import json
import shutil
from pathlib import Path


PATTERNS = {
    "gene_counts.raw.tsv": ["*gene_counts*.tsv", "*counts_gene*.tsv", "*featureCounts*.txt"],
    "gene_tpm.tsv": ["*gene_tpm*.tsv", "*abundance*gene*.tsv", "*salmon*gene*T*"],
    "transcript_tpm.tsv": ["*transcript_tpm*.tsv", "*abundance.tsv"],
    "multiqc_report.html": ["multiqc_report.html", "*multiqc*.html"],
}


def first_match(root, patterns):
    for pattern in patterns:
        hits = sorted(root.rglob(pattern))
        if hits:
            return hits[0]
    return None


def main():
    p = argparse.ArgumentParser(description="Collect nf-core/rnaseq outputs into BioMind crop RNA-seq layout.")
    p.add_argument("--nfcore-dir", required=True)
    p.add_argument("--outdir", required=True)
    args = p.parse_args()
    root = Path(args.nfcore_dir)
    out = Path(args.outdir)
    counts = out / "counts"
    qc = out / "qc"
    handoff = out / "handoff"
    repro = out / "reproducibility"
    for d in (counts, qc, handoff, repro):
        d.mkdir(parents=True, exist_ok=True)

    found = {}
    for target, patterns in PATTERNS.items():
        src = first_match(root, patterns)
        found[target] = str(src) if src else None
        if src:
            dest = (qc if target.endswith(".html") else counts) / target
            shutil.copy2(src, dest)

    count_for_de = counts / "gene_counts.raw.tsv"
    if count_for_de.exists():
        shutil.copy2(count_for_de, handoff / "count_matrix_for_de.tsv")
    (handoff / "sample_metadata.template.tsv").write_text(
        "sample_id\tgenotype\ttissue\tcondition\treplicate\tbatch\tstage\tlocation\tyear\n",
        encoding="utf-8",
    )
    (handoff / "crop_rnaseq_de.command.sh").write_text(
        "python ../crop-rnaseq-de/scripts/run_tpm_fc_filter.py --tpm counts/gene_tpm.tsv "
        "--metadata handoff/sample_metadata.template.tsv --contrast condition,treatment,control --outdir rnaseq_de\n",
        encoding="utf-8",
    )
    (out / "result.json").write_text(json.dumps({"found": found}, indent=2), encoding="utf-8")
    (out / "report.md").write_text("# Crop RNA-seq Pipeline Output\n\nParsed nf-core/rnaseq outputs.\n", encoding="utf-8")
    print(f"parsed outputs -> {out}")


if __name__ == "__main__":
    main()
