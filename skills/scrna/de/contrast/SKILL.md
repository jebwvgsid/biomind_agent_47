---
name: scRNA DE Contrast
description: 在样本分组、条件分组或指定细胞类型子集上执行差异表达并导出结果表与火山图。Use when Codex needs a manifest-driven differential expression step after scRNA annotation or clustering.
tags: [single-cell, scRNA, Seurat, differential-expression, contrast, 单细胞, 差异表达, 对比]
task_types: [single_cell_rnaseq]
stage_tags: [de_analysis]
inputs: [annotated_seurat_object, clustered_seurat_object, contrast_definition]
outputs: [de_table, volcano_plot]
when_to_use:
  - Workflow stage is de_analysis.
  - User provides sample groups, conditions, or cell-type subset contrast.
when_not_to_use: [Bulk RNA-seq, no contrast definition]
legacy_ids: [scrna-de-contrast]
runtime:
  default_env: bio_scrna_core
  env_runner: conda run -n bio_scrna_core Rscript
  language: r
  packages: [Seurat, yaml, ggplot2, dplyr]
  capabilities: [scrna-de, scrna-contrast]
  install_tool: '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>'
---

# scRNA DE Contrast

Use this skill to compute one differential-expression contrast from a Seurat
object.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_annotated.rds`, or explicit `--input-rds`

## Optional Manifest Fields

- `de.group_by`
- `de.ident_1`
- `de.ident_2`
- `de.subset_column`
- `de.subset_value`
- `de.test_use`
- `de.min_pct`
- `de.logfc_threshold`
- `de.latent_vars`

## Quick Start

```bash
conda run -n bio_scrna_core Rscript skills/scrna/de/contrast/scripts/de_contrast_seurat.R --manifest /abs/path/manifest.yaml
```

## Environment

Reuse `bio_scrna_core`. If the environment is missing or package verification
fails, delegate installation with:

```text
<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>
```

Do not inline package-manager installation commands from this skill.

## Outputs

- `{output_dir}/tables/de_results.tsv`
- `{output_dir}/tables/de_summary.tsv`
- `{output_dir}/plots/de_volcano.png` when results are available

## Rules

- Prefer `sample_id` as the default grouping column when exactly two groups are
  present.
- If no explicit labels are supplied and the grouping column has exactly two
  levels, use those two levels as the contrast automatically.
- Keep DE separate from cluster marker detection.
