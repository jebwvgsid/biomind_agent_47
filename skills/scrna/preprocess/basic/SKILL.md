---
name: scRNA Preprocess Basic
description: 对 QC 后的 Seurat 对象执行标准化、高变基因筛选和缩放，作为下游 PCA、UMAP 和聚类的统一预处理步骤。Use when Codex needs a manifest-driven preprocessing step after scRNA QC.
tags: [single-cell, scRNA, Seurat, preprocess, normalization, variable-features, 单细胞, 预处理, 标准化]
task_types: [single_cell_rnaseq]
stage_tags: [preprocess]
inputs: [qc_filtered_seurat_object]
outputs: [preprocessed_seurat_object]
when_to_use:
  - Workflow stage is preprocess.
  - User needs normalization, variable features, and scaling before PCA or clustering.
when_not_to_use: [Bulk RNA-seq, raw unfiltered matrix]
legacy_ids: [scrna-preprocess-basic]
runtime:
  default_env: bio_scrna_core
  env_runner: conda run -n bio_scrna_core Rscript
  language: r
  packages: [Seurat, yaml, ggplot2, dplyr]
  capabilities: [scrna-preprocess, scrna-normalize, scrna-variable-features]
  install_tool: '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>'
---

# scRNA Preprocess Basic

Use this skill to normalize a QC-filtered Seurat object and save a stable
checkpoint for downstream clustering.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_qc.rds`, or explicit `--input-rds`

## Optional Manifest Fields

- `preprocess.method`
  - `lognorm` (default)
  - `sctransform`
- `preprocess.nfeatures`
- `preprocess.scale_features`
  - `variable` (default)
  - `all`
- `preprocess.regress_vars`

## Quick Start

```bash
conda run -n bio_scrna_core Rscript skills/scrna/preprocess/basic/scripts/preprocess_scrna_seurat.R --manifest /abs/path/manifest.yaml
```

## Environment

Reuse `bio_scrna_core`. If the environment is missing or package verification
fails, delegate installation with:

```text
<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>
```

Do not inline package-manager installation commands from this skill.

## Outputs

- `{output_dir}/objects/seurat_norm.rds`
- `{output_dir}/tables/variable_features.tsv`
- `{output_dir}/tables/preprocess_summary.tsv`
- `{output_dir}/plots/variable_features.png`

## Rules

- Reuse the same manifest used for ingest and QC.
- Default to Seurat classic preprocessing:
  `NormalizeData -> FindVariableFeatures -> ScaleData`.
- Only use `SCTransform` when the manifest explicitly requests it.
- Keep checkpoint names stable; do not create extra PCA-only RDS files.
