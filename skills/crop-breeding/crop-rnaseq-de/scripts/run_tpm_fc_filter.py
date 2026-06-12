#!/usr/bin/env python3
import argparse
import json
import math
from pathlib import Path
import pandas as pd


def read_table(path):
    return pd.read_csv(path, sep=None, engine="python")


def main():
    p = argparse.ArgumentParser(description="No-replicate crop RNA-seq TPM fold-change candidate screening.")
    p.add_argument("--tpm", required=True, help="Gene TPM matrix with gene_id plus sample columns.")
    p.add_argument("--metadata", required=True, help="Sample metadata with sample_id and contrast factor.")
    p.add_argument("--contrast", required=True, help="factor,treatment,control; positive log2FC means up in treatment.")
    p.add_argument("--outdir", required=True)
    p.add_argument("--min-tpm", type=float, default=1.0)
    p.add_argument("--min-abs-log2fc", type=float, default=1.0)
    args = p.parse_args()

    factor, treatment, control = args.contrast.split(",")
    tpm = read_table(args.tpm)
    meta = read_table(args.metadata)
    if "gene_id" not in tpm.columns:
        tpm = tpm.rename(columns={tpm.columns[0]: "gene_id"})
    if "sample_id" not in meta.columns:
        raise SystemExit("metadata must contain sample_id")
    if factor not in meta.columns:
        raise SystemExit(f"metadata missing contrast factor: {factor}")

    treat_samples = meta.loc[meta[factor].astype(str) == treatment, "sample_id"].astype(str).tolist()
    ctrl_samples = meta.loc[meta[factor].astype(str) == control, "sample_id"].astype(str).tolist()
    missing = [s for s in treat_samples + ctrl_samples if s not in tpm.columns]
    if missing:
        raise SystemExit(f"samples missing from TPM matrix: {', '.join(missing)}")
    if not treat_samples or not ctrl_samples:
        raise SystemExit("contrast groups must both contain samples")

    out = Path(args.outdir)
    tables = out / "tables"
    repro = out / "reproducibility"
    handoff = out / "handoff"
    for d in (tables, repro, handoff):
        d.mkdir(parents=True, exist_ok=True)

    res = pd.DataFrame({"gene_id": tpm["gene_id"]})
    res[f"mean_tpm_{treatment}"] = tpm[treat_samples].mean(axis=1)
    res[f"mean_tpm_{control}"] = tpm[ctrl_samples].mean(axis=1)
    res["log2FoldChange"] = (res[f"mean_tpm_{treatment}"] + 1).apply(math.log2) - (res[f"mean_tpm_{control}"] + 1).apply(math.log2)
    res["abs_log2FC"] = res["log2FoldChange"].abs()
    res["direction"] = res["log2FoldChange"].map(lambda x: f"up_in_{treatment}" if x > 0 else f"up_in_{control}" if x < 0 else "unchanged")
    res["screening_pass"] = (
        (res[[f"mean_tpm_{treatment}", f"mean_tpm_{control}"]].max(axis=1) >= args.min_tpm)
        & (res["abs_log2FC"] >= args.min_abs_log2fc)
    )
    res["mode"] = "no_replicate_tpm_fc_screen"
    res["warning"] = "No biological replicate statistical test was run; candidates are screening results, not strict DEG."
    full = tables / "de_results.full.tsv"
    sig = tables / "de_results.significant.tsv"
    screen = tables / "no_replicate_fc_screen.tsv"
    res.to_csv(full, sep="\t", index=False)
    res.loc[res["screening_pass"]].to_csv(sig, sep="\t", index=False)
    res.to_csv(screen, sep="\t", index=False)
    (tables / "qc_summary.tsv").write_text(
        f"contrast\t{args.contrast}\nmode\tno_replicate_tpm_fc_screen\nn_treatment\t{len(treat_samples)}\nn_control\t{len(ctrl_samples)}\n",
        encoding="utf-8",
    )
    (handoff / "crop_diff_visualizer.command.sh").write_text(
        f"Rscript ../crop-diff-visualizer/scripts/plot_volcano.R --de-table {full} --outdir {out / 'figures'}\n",
        encoding="utf-8",
    )
    (out / "result.json").write_text(json.dumps({"mode": "no_replicate_tpm_fc_screen", "significant": int(res["screening_pass"].sum())}, indent=2), encoding="utf-8")
    (out / "report.md").write_text(
        "# Crop RNA-seq Candidate Screening\n\nWARNING: no biological replicate statistical DEG test was run. Results are TPM fold-change candidates.\n",
        encoding="utf-8",
    )
    print(f"wrote {full}")


if __name__ == "__main__":
    main()
