---
name: crop-diff-visualizer
description: Visualize and summarize crop differential expression or marker tables with volcano plots, MA plots, heatmaps, top DEG bars, candidate labels, and Markdown/HTML reports. Use after crop-rnaseq-de or when the user already has DE/marker tables; do not recompute differential expression.
---

# Crop Diff Visualizer

Use this skill only for downstream visualization and candidate reporting:

```text
DE table + TPM + metadata + candidate list -> figures + highlighted gene tables + report
```

## Inputs

- `--de-table de_results.annotated.tsv`
- `--tpm-matrix gene_tpm.tsv`
- `--metadata sample_metadata.tsv`
- `--candidate-gene-list candidate_genes.txt`
- `--highlight-keywords sucrose,photosynthesis,transporter,starch,kinase`
- Optional sample color mapping for labels such as `18l,18s,175l,175s`.

## Workflow

1. Auto-detect columns: `gene_id`, `log2FoldChange`, `pvalue`, `padj`, `baseMean`, `mean_tpm`, `annotation`.
2. Detect no-replicate screening tables; use `abs_log2FC` and TPM thresholds without calling padj-based significance strict DEG.
3. Generate volcano, MA plot, top up/down barplot, heatmap, and candidate gene expression plot.
4. Highlight genes matching candidate list or crop trait keywords.
5. Write `report.md`, `report.html`, and `result.json`.

## Outputs

- `figures/volcano.labelled.png`
- `figures/ma_plot.png`
- `figures/top_up_down_genes.barplot.png`
- `figures/top_deg_heatmap.png`
- `figures/sample_correlation.png`
- `figures/candidate_gene_expression.png`
- `tables/top_genes.tsv`
- `tables/significant_genes.tsv`
- `tables/highlighted_candidate_genes.tsv`
- `tables/figure_gene_labels.tsv`

## Gotchas

- Do not rerun DESeq2, edgeR, or fold-change analysis here.
- When no-replicate mode is detected, label outputs as screening candidates.
- Prefer functional keyword labels for crop biology: sugar, sucrose, starch, photosynthesis, transporter, kinase, disease, flowering, yield.

## Validation Boundary

Do not keep local example matrices inside this skill. Validate report rendering with independent validation data or user-provided analysis outputs.
