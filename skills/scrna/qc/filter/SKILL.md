---
name: scRNA QC Filter
description: 对 Seurat 单细胞对象执行质控统计、percent.mt 计算、阈值过滤和 QC 图导出。用于课程第4天的质控过滤步骤，也适用于其他样本的可重复 QC。Use when Codex needs a reusable QC step for scRNA-seq data before downstream normalization and clustering.
tags: [single-cell, scRNA, Seurat, QC, filtering, 单细胞, 质控]
legacy_ids: [scrna-qc-filter]
runtime:
  default_env: bio_scrna_core
  env_runner: conda run -n bio_scrna_core Rscript
  language: r
  packages: [Seurat, yaml, ggplot2, patchwork]
  capabilities: [scrna-qc, scrna-filter]
  install_command: conda create -n bio_scrna_core -c conda-forge -c bioconda r-base=4.3 r-seurat r-yaml r-ggplot2 r-patchwork -y
---

# scRNA QC Filter

Use this skill to run a manifest-driven QC step on an existing Seurat object.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_raw.rds` produced by the ingest step, or
- explicit `--input-rds` path

## Optional Manifest QC Fields

- `mt_pattern`
- `qc.min_features`
- `qc.max_features`
- `qc.min_counts`
- `qc.max_counts`
- `qc.max_percent_mt`

## Quick Start

```bash
conda run -n bio_scrna_core Rscript skills/scrna/qc/filter/scripts/qc_filter_seurat.R --manifest /abs/path/manifest.yaml
```

## Outputs

- `{output_dir}/objects/seurat_qc.rds`
- `{output_dir}/tables/qc_summary.tsv`
- `{output_dir}/tables/qc_metrics.tsv`
- `{output_dir}/plots/qc_violin.png`
- `{output_dir}/plots/qc_scatter.png`

## Rules

- Compute `percent.mt` if it does not already exist.
- Use manifest thresholds instead of hardcoding values in the script call.
- Preserve metadata columns and append QC metrics rather than dropping them.
- Export `qc_metrics.tsv` for all cells, not only retained cells, and include
  `pass_qc`, per-threshold pass flags, and threshold values for auditability.
