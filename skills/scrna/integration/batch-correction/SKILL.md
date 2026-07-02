---
name: scRNA Integration Batch Correction
description: 对多样本 scRNA Seurat 对象执行批次整合或批次效应校正，支持 Seurat anchor/RPCA/CCA 或 Harmony 决策。Use when a project has multiple samples/batches and needs integrated embeddings before clustering or annotation; do not integrate when batch and biological condition are confounded without asking the user.
tags: [single-cell, scRNA, Seurat, integration, batch-correction, Harmony, anchors, 单细胞, 批次校正]
task_types: [single_cell_rnaseq]
stage_tags: [integration, batch_correction]
inputs: [qc_filtered_seurat_object, preprocessed_seurat_object, sample_metadata, batch_column]
outputs: [integrated_seurat_object, integration_qc_plots]
when_to_use:
  - User asks for multi-sample integration, batch correction, Harmony, CCA, RPCA, or anchor-based integration.
  - Workflow stage is integration or batch_correction.
when_not_to_use:
  - Single-sample analysis without known batch effect.
  - Batch is perfectly confounded with the biological contrast and no user decision is available.
legacy_ids: [scrna-batch-correction, scrna-integrate]
runtime:
  default_env: bio_scrna_core
  env_runner: conda run -n bio_scrna_core Rscript
  language: r
  packages: [Seurat, yaml, ggplot2, patchwork, dplyr]
  capabilities: [scrna-integration, scrna-batch-correction]
  install_tool: '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>'
---

# scRNA Integration Batch Correction

Use this skill for multi-sample scRNA integration before clustering,
annotation, or cross-condition comparison.

## Required Inputs

- Manifest YAML
- QC-filtered or preprocessed Seurat RDS
- Metadata column identifying sample/batch

## Optional Manifest Fields

- `integration.method`
  - `seurat_anchors`
  - `rpca`
  - `cca`
  - `harmony`
- `integration.batch_column`
- `integration.condition_column`
- `integration.dims`
- `integration.nfeatures`

## Workflow

1. Verify sample/batch metadata and whether there are at least two groups.
2. Inspect whether batch and biological condition are confounded.
3. Choose method only from explicit user request or manifest.
4. Generate integrated object and before/after embedding QC.
5. Preserve raw RNA assay and original metadata for downstream DE.

## Outputs

- `{output_dir}/objects/seurat_integrated.rds`
- `{output_dir}/tables/integration_summary.tsv`
- `{output_dir}/plots/umap_before_integration.png`
- `{output_dir}/plots/umap_after_integration.png`

## Ask User Before Running

Ask the user when any of these are unclear:

- Which metadata column is batch/sample/library.
- Which metadata column is the biological condition.
- Whether to integrate all samples together or within tissue/cell-type subsets.
- Whether batch correction is appropriate when condition and batch are
  confounded.

Do not treat every `orig.ident` difference as a removable batch effect.

