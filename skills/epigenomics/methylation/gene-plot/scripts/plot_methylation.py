#!/usr/bin/env python3
"""
Methylation Profile Plot Script

Generates per-gene CG/CHG/CHH methylation profile panels (upstream → gene body
→ downstream) combined with expression (TPM) and chromatin compartment labels.

Output per gene: a 3×3 PNG panel + a TSV summary table.

Usage:
    python plot_methylation.py \
        --gff genome.gff3 \
        --tpm gene_tpm.csv \
        --outdir ./plots \
        --genes GT21.04g7G0018420 GT21.07g3G0008680 \
        --sample 175l /path/CG.bw /path/CHG.bw /path/CHH.bw /path/comp.bed \
        --sample 175s /path/CG.bw /path/CHG.bw /path/CHH.bw /path/comp.bed

Requirements:
    pyBigWig pandas numpy matplotlib
"""

import argparse
import os
import re
import sys
from pathlib import Path

def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Plot per-gene methylation profiles from bigWig files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
  python plot_methylation.py \\
    --gff genome.gff3 \\
    --tpm gene_tpm.csv \\
    --outdir ./plots \\
    --genes GT21.04g7G0018420 GT21.07g3G0008680 \\
    --sample 175l CG.bw CHG.bw CHH.bw comp.bed \\
    --sample 18l  CG.bw CHG.bw CHH.bw comp.bed
        """,
    )

    parser.add_argument("--gff",     required=True, help="GFF3 annotation file")
    parser.add_argument("--tpm",     required=True, help="TPM CSV (first col = gene ID)")
    parser.add_argument("--outdir",  required=True, help="Output directory")
    parser.add_argument("--genes",   nargs="+", required=True, help="Gene IDs to plot")

    parser.add_argument(
        "--sample", nargs=5, action="append", metavar=("LABEL", "CG_BW", "CHG_BW", "CHH_BW", "COMP_BED"),
        required=True,
        help="Add a sample: LABEL CG.bw CHG.bw CHH.bw compartment.bed (repeat for each sample)",
    )

    # AGENT-EDITABLE: window / binning parameters
    parser.add_argument("--upstream",    type=int, default=2000)
    parser.add_argument("--downstream",  type=int, default=2000)
    parser.add_argument("--nbins-up",    type=int, default=20)
    parser.add_argument("--nbins-body",  type=int, default=40)
    parser.add_argument("--nbins-down",  type=int, default=20)
    parser.add_argument("--dpi",         type=int, default=300)

    return parser.parse_args()

# Help describes the real CLI without importing plotting dependencies.
if __name__ == "__main__" and any(flag in sys.argv[1:] for flag in ("--help", "-h")):
    parse_arguments()

import numpy as np
import pandas as pd

try:
    import pyBigWig
except ImportError:
    print('Error: pyBigWig not installed. Delegate installation with: ask the user before installing the required dependency: methylation-gene-plot_python_deps')
    sys.exit(1)

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    print('Error: matplotlib not installed. Delegate installation with: ask the user before installing the required dependency: methylation-gene-plot_python_deps')
    sys.exit(1)


# ── Utilities ─────────────────────────────────────────────────────────────────

def clean_gene_id(x):
    x = str(x).strip()
    x = re.sub(r"^(gene:|transcript:|ID=|rna-)", "", x)
    x = re.sub(r";.*$", "", x)
    x = re.sub(r"\.t\d+$", "", x)
    return x


def parse_gff_gene(gff_file):
    rows = []
    with open(gff_file) as f:
        for line in f:
            if line.startswith("#"):
                continue
            arr = line.rstrip("\n").split("\t")
            if len(arr) < 9:
                continue
            chrom, _, feature, start, end, _, strand, _, attrs = arr
            if feature != "gene":
                continue
            m = re.search(r"ID=([^;]+)", attrs)
            if not m:
                continue
            rows.append({
                "gene_id": clean_gene_id(m.group(1)),
                "chr": chrom,
                "start": int(start),
                "end": int(end),
                "strand": strand,
            })
    return pd.DataFrame(rows)


def load_tpm(path, sample_keys):
    """Load TPM CSV, auto-detect sample columns by label substring."""
    df = pd.read_csv(path)
    gene_col = df.columns[0]
    df["gene_id"] = df[gene_col].map(clean_gene_id)

    matched = {}
    for key in sample_keys:
        for col in df.columns:
            if key.lower() in col.lower():
                matched[key] = col
                break

    if not matched:
        raise ValueError(
            f"No sample columns matched {sample_keys}.\n"
            f"Available columns: {list(df.columns)}"
        )

    out = df[["gene_id"] + list(matched.values())].copy()
    out.columns = ["gene_id"] + list(matched.keys())
    out = out.groupby("gene_id", as_index=False)[list(matched.keys())].sum()
    for k in matched:
        out[k] = pd.to_numeric(out[k], errors="coerce").fillna(0.0)
    return out


def fetch_mean(bw, chrom, start, end):
    start, end = max(0, int(start)), max(int(start) + 1, int(end))
    vals = np.array(bw.values(chrom, start, end, numpy=True))
    vals = vals[np.isfinite(vals)]
    return float(np.nanmean(vals)) if vals.size else np.nan


def fetch_binned(bw, chrom, start, end, nbins):
    start, end = max(0, int(start)), max(int(start) + 1, int(end))
    edges = np.linspace(start, end, nbins + 1, dtype=int)
    result = []
    for i in range(nbins):
        s, e = edges[i], max(edges[i + 1], edges[i] + 1)
        vals = np.array(bw.values(chrom, s, e, numpy=True))
        vals = vals[np.isfinite(vals)]
        result.append(float(np.nanmean(vals)) if vals.size else np.nan)
    return np.array(result, dtype=float)


def get_regions(row, upstream, downstream):
    gs, ge = int(row["start"]) - 1, int(row["end"])
    if row["strand"] == "+":
        return (max(0, gs - upstream), gs), (gs, ge), (ge, ge + downstream)
    else:
        return (ge, ge + upstream), (gs, ge), (max(0, gs - downstream), gs)


def get_compartment(bed_path, chrom, start, end):
    if not os.path.exists(bed_path):
        return "NA"
    labels = []
    with open(bed_path) as f:
        for line in f:
            arr = line.rstrip("\n").split("\t")
            if len(arr) < 4:
                continue
            c, s, e, lab = arr[0], int(arr[1]), int(arr[2]), arr[3]
            if c == chrom and not (e <= start or s >= end):
                labels.append(lab)
    if not labels:
        return "NA"
    seen, uniq = set(), []
    for x in labels:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
    return ",".join(uniq)


# ── Plotting ──────────────────────────────────────────────────────────────────

# AGENT-EDITABLE: figure layout
def make_figure(gene, chrom, row, strand, plot_start, plot_end,
                profiles, region_means, comp_notes, expr,
                sample_keys, contexts, nbins_up, nbins_body, nbins_down,
                outdir, dpi):

    fig = plt.figure(figsize=(13, 9))
    # AGENT-EDITABLE: grid layout ratios
    gs = fig.add_gridspec(3, 3, height_ratios=[1.1, 0.9, 0.9])

    axes_r0 = [fig.add_subplot(gs[0, i]) for i in range(3)]
    ax4, ax5, ax6 = [fig.add_subplot(gs[1, i]) for i in range(3)]
    ax7, ax8, ax9 = [fig.add_subplot(gs[2, i]) for i in range(3)]

    x = np.arange(nbins_up + nbins_body + nbins_down)

    # Row 0: line profiles
    for ctx, ax in zip(contexts, axes_r0):
        for s in sample_keys:
            ax.plot(x, profiles[ctx][s], marker="o", lw=1, ms=3, label=s)
        ax.axvline(nbins_up - 0.5, linestyle="--", lw=0.8, color="gray")
        ax.axvline(nbins_up + nbins_body - 0.5, linestyle="--", lw=0.8, color="gray")
        ax.set_title(f"{ctx} profile")
        ax.set_ylabel("Methylation")
        ax.set_xlabel("Upstream → Gene body → Downstream")
    axes_r0[2].legend(fontsize=8, loc="upper right")

    # AGENT-EDITABLE: bar chart helper
    def bar_ctx(ax, ctx):
        cats = ["upstream", "body", "downstream"]
        bx = np.arange(len(cats))
        w = 0.18
        for i, s in enumerate(sample_keys):
            vals = [region_means[ctx][s][c] for c in cats]
            ax.bar(bx + (i - (len(sample_keys) - 1) / 2) * w, vals, width=w, label=s)
        ax.set_xticks(bx)
        ax.set_xticklabels(cats)
        ax.set_ylabel("Mean methylation")
        ax.set_title(f"{ctx} regional mean")

    bar_ctx(ax4, "CG")
    bar_ctx(ax5, "CHG")
    bar_ctx(ax7, "CHH")

    # Expression bar
    # AGENT-EDITABLE: expression panel
    expr_vals = [expr.get(s, np.nan) for s in sample_keys]
    ax6.bar(np.arange(len(sample_keys)), expr_vals)
    ax6.set_xticks(np.arange(len(sample_keys)))
    ax6.set_xticklabels(sample_keys, rotation=30, ha="right")
    ax6.set_title("Expression (TPM)")
    ax6.set_ylabel("TPM")

    for ax in [ax8, ax9]:
        ax.axis("off")

    ax8.text(
        0.5, 0.5,
        "Compartment labels\n\n" + "\n".join(f"{k}: {v}" for k, v in comp_notes.items()),
        ha="center", va="center", fontsize=9, transform=ax8.transAxes,
    )
    ax9.text(
        0.5, 0.5,
        (f"Gene: {gene}\nChr: {chrom}\n"
         f"Start: {int(row['start'])-1}\nEnd: {row['end']}\n"
         f"Strand: {strand}\nWindow: {plot_start}-{plot_end}"),
        ha="center", va="center", fontsize=9, transform=ax9.transAxes,
    )

    fig.suptitle(gene, fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.97])

    out = os.path.join(outdir, f"{gene}.CG_CHG_CHH.methylation_expression.png")
    fig.savefig(out, dpi=dpi)
    plt.close(fig)
    print(f"[INFO] saved: {out}")
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_arguments()

    os.makedirs(args.outdir, exist_ok=True)
    contexts = ["CG", "CHG", "CHH"]

    # Build sample dict
    samples = {}
    for label, cg, chg, chh, bed in args.sample:
        for path in [cg, chg, chh]:
            if not os.path.exists(path):
                print(f"[ERROR] File not found: {path}")
                sys.exit(1)
        samples[label] = {"compartment_bed": bed, "bw": {"CG": cg, "CHG": chg, "CHH": chh}}

    sample_keys = list(samples.keys())

    # Load annotations
    gene_df  = parse_gff_gene(args.gff)
    gene_map = gene_df.set_index("gene_id").to_dict("index")

    expr_df  = load_tpm(args.tpm, sample_keys)
    expr_map = expr_df.set_index("gene_id").to_dict("index")

    # Open bigWigs
    opened = {}
    for s, info in samples.items():
        opened[s] = {ctx: pyBigWig.open(path) for ctx, path in info["bw"].items()}

    summary_rows = []

    for gene in args.genes:
        if gene not in gene_map:
            print(f"[WARN] {gene} not found in GFF — skipping")
            continue

        row    = gene_map[gene]
        chrom  = row["chr"]
        strand = row["strand"]
        up, body, down = get_regions(row, args.upstream, args.downstream)
        plot_start = min(up[0], body[0], down[0])
        plot_end   = max(up[1], body[1], down[1])

        profiles     = {ctx: {} for ctx in contexts}
        region_means = {ctx: {} for ctx in contexts}
        comp_notes   = {}

        for s, info in samples.items():
            comp_notes[s] = get_compartment(info["compartment_bed"], chrom, plot_start, plot_end)
            for ctx in contexts:
                bw = opened[s][ctx]
                profiles[ctx][s] = np.concatenate([
                    fetch_binned(bw, chrom, up[0],   up[1],   args.nbins_up),
                    fetch_binned(bw, chrom, body[0], body[1], args.nbins_body),
                    fetch_binned(bw, chrom, down[0], down[1], args.nbins_down),
                ])
                region_means[ctx][s] = {
                    "upstream":   fetch_mean(bw, chrom, up[0],   up[1]),
                    "body":       fetch_mean(bw, chrom, body[0], body[1]),
                    "downstream": fetch_mean(bw, chrom, down[0], down[1]),
                }

        expr = expr_map.get(gene, {k: np.nan for k in sample_keys})

        make_figure(
            gene=gene, chrom=chrom, row=row, strand=strand,
            plot_start=plot_start, plot_end=plot_end,
            profiles=profiles, region_means=region_means,
            comp_notes=comp_notes, expr=expr,
            sample_keys=sample_keys, contexts=contexts,
            nbins_up=args.nbins_up, nbins_body=args.nbins_body,
            nbins_down=args.nbins_down, outdir=args.outdir, dpi=args.dpi,
        )

        summary_rows.append({
            "gene_id": gene, "chr": chrom,
            "start0": int(row["start"]) - 1, "end": row["end"], "strand": strand,
            **{f"expr_{s}": expr.get(s, np.nan) for s in sample_keys},
            **{f"compartment_{s}": comp_notes[s] for s in sample_keys},
        })

    for s in opened:
        for ctx in opened[s]:
            opened[s][ctx].close()

    summary_path = os.path.join(args.outdir, "methylation_plot_summary.tsv")
    pd.DataFrame(summary_rows).to_csv(summary_path, sep="\t", index=False)
    print(f"[INFO] Summary: {summary_path}")
    print("[INFO] Done.")


if __name__ == "__main__":
    main()
