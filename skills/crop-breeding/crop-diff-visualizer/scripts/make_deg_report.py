#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import pandas as pd


def read_table(path):
    return pd.read_csv(path, sep=None, engine="python")


def main():
    p = argparse.ArgumentParser(description="Create crop DEG candidate tables and Markdown/HTML reports.")
    p.add_argument("--de-table", required=True)
    p.add_argument("--outdir", required=True)
    p.add_argument("--candidate-gene-list")
    p.add_argument("--highlight-keywords", default="")
    p.add_argument("--top-n", type=int, default=50)
    args = p.parse_args()
    out = Path(args.outdir)
    tables = out / "tables"
    tables.mkdir(parents=True, exist_ok=True)
    de = read_table(args.de_table)
    if "gene_id" not in de.columns:
        de = de.rename(columns={de.columns[0]: "gene_id"})
    lfc = "log2FoldChange" if "log2FoldChange" in de.columns else "logFC"
    de["abs_lfc_for_rank"] = de[lfc].abs()
    top = de.sort_values("abs_lfc_for_rank", ascending=False).head(args.top_n)
    top.to_csv(tables / "top_genes.tsv", sep="\t", index=False)
    sig = de
    if "padj" in de.columns:
        sig = de.loc[de["padj"].fillna(1) < 0.05]
    elif "screening_pass" in de.columns:
        sig = de.loc[de["screening_pass"].astype(str).str.lower().isin(["true", "1"])]
    sig.to_csv(tables / "significant_genes.tsv", sep="\t", index=False)
    candidates = set()
    if args.candidate_gene_list:
        candidates = {x.strip() for x in Path(args.candidate_gene_list).read_text(encoding="utf-8").splitlines() if x.strip()}
    keywords = [x.strip().lower() for x in args.highlight_keywords.split(",") if x.strip()]
    text_cols = [c for c in de.columns if any(k in c.lower() for k in ["annotation", "description", "kegg", "pfam", "swiss", "nr"])]
    mask = de["gene_id"].isin(candidates)
    if keywords and text_cols:
        blob = de[text_cols].fillna("").astype(str).agg(" ".join, axis=1).str.lower()
        mask = mask | blob.apply(lambda s: any(k in s for k in keywords))
    highlighted = de.loc[mask].copy()
    highlighted.to_csv(tables / "highlighted_candidate_genes.tsv", sep="\t", index=False)
    top[["gene_id", lfc]].to_csv(tables / "figure_gene_labels.tsv", sep="\t", index=False)
    report = out / "report.md"
    report.write_text(
        f"# Crop DEG Visualization Report\n\nTop genes: {len(top)}\n\nSignificant/screening genes: {len(sig)}\n\nHighlighted candidates: {len(highlighted)}\n",
        encoding="utf-8",
    )
    (out / "report.html").write_text(report.read_text(encoding="utf-8").replace("\n", "<br>\n"), encoding="utf-8")
    (out / "result.json").write_text(json.dumps({"top_genes": len(top), "significant": len(sig), "highlighted": len(highlighted)}, indent=2), encoding="utf-8")
    print(f"wrote report -> {report}")


if __name__ == "__main__":
    main()
