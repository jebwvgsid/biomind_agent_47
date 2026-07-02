---
name: scRNA SCTransform Preprocess
description: 对 QC 后的 Seurat 对象执行 SCTransform 标准化、高变基因建模和可选协变量回归。Use when the user explicitly requests SCTransform/SCT or a workflow stage selects SCT preprocessing after scRNA QC; do not use as the default log-normalization path unless requested or justified by the workflow.
tags: [single-cell, scRNA, Seurat, SCTransform, SCT, normalization, 单细胞, 预处理]
task_types: [single_cell_rnaseq]
stage_tags: [sctransform_preprocess, preprocess]
inputs: [qc_filtered_seurat_object, regression_variables]
outputs: [sct_seurat_object, variable_features, preprocess_summary]
when_to_use:
  - User asks for SCTransform, SCT, variance-stabilizing normalization, or regression-aware preprocessing.
  - Workflow stage is sctransform_preprocess.
when_not_to_use:
  - User requested classic NormalizeData/FindVariableFeatures/ScaleData only.
  - The SCT package is unavailable and the user did not approve environment repair.
legacy_ids: [scrna-sctransform, scrna-sct-preprocess]
runtime:
  default_env: bio_scrna_core
  env_runner: conda run -n bio_scrna_core Rscript
  language: r
  packages: [Seurat, sctransform, yaml, ggplot2, dplyr]
  capabilities: [scrna-sctransform, scrna-preprocess]
  install_tool: '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>'
---

# scRNA SCTransform Preprocess

Use this skill for the tutorial's SCTransform preprocessing branch. It reuses
the existing preprocessing script with `preprocess.method: sctransform`.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_qc.rds`, or explicit `--input-rds`

## Required Manifest Fields

The manifest must set:

```yaml
preprocess:
  method: sctransform
```

Recommended fields:

- `preprocess.nfeatures`
- `preprocess.regress_vars`

## Quick Start

```bash
conda run -n bio_scrna_core Rscript skills/scrna/preprocess/basic/scripts/preprocess_scrna_seurat.R --manifest /abs/path/manifest.yaml --input-rds /abs/path/seurat_qc.rds --output-rds /abs/path/objects/seurat_sct.rds
```

## Environment

Reuse `bio_scrna_core`. If `sctransform` is missing, delegate installation with:

```text
<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>
```

Do not inline package-manager installation commands from this skill.

## Outputs

- `{output_dir}/objects/seurat_sct.rds` when `--output-rds` is supplied
- `{output_dir}/tables/variable_features.tsv`
- `{output_dir}/tables/preprocess_summary.tsv`
- `{output_dir}/plots/variable_features.png`

## Ask User Before Running

Ask the user when any of these are unclear:

- Whether SCT or classic log-normalization should be used.
- Which covariates should be regressed, especially `percent.mt`, cell-cycle
  scores, sample/batch columns, or chemistry/library columns.
- Whether batch correction should happen before or after SCT in a multi-sample
  project.

Never infer biological covariates from column names alone when the project
background is ambiguous.

