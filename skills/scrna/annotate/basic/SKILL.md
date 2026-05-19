---
name: scRNA Annotate Basic
description: 基于 cluster 标签映射、marker 规则或显式 SingleR reference 对聚类结果进行细胞类型注释，并保存带 cell_type 的 Seurat 对象。Use when Codex needs a manifest-driven cell annotation step after scRNA clustering.
tags: [single-cell, scRNA, Seurat, annotation, cell-type, SingleR, 单细胞, 注释, 细胞类型]
task_types: [single_cell_rnaseq]
stage_tags: [annotation]
inputs: [clustered_seurat_object, marker_table, annotation_reference, marker_rules]
outputs: [annotated_seurat_object]
when_to_use:
  - Workflow stage is annotation.
  - User has cluster labels plus marker rules, explicit label mapping, or SingleR reference.
when_not_to_use: [Bulk RNA-seq, no clustering result]
legacy_ids: [scrna-annotate-basic]
runtime:
  default_env: bio_scrna_core
  env_runner: conda run -n bio_scrna_core Rscript
  language: r
  packages: [Seurat, yaml, ggplot2, dplyr]
  capabilities: [scrna-annotation, scrna-celltype]
  install_tool: '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>'
---

# scRNA Annotate Basic

Use this skill to add a `cell_type` column to a clustered Seurat object.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_clustered.rds`, or explicit `--input-rds`

## Optional Manifest Fields

- `annotation.method`
  - `marker-first` (default)
  - `singler`
- `annotation.cluster_labels`
- `annotation.marker_rules`
- `annotation.reference_rds`
- `annotation.reference_builtin`
- `annotation.reference_labels_column`

## Quick Start

```bash
conda run -n bio_scrna_core Rscript skills/scrna/annotate/basic/scripts/annotate_scrna_seurat.R --manifest /abs/path/manifest.yaml
```

## Environment

Reuse `bio_scrna_core`. If the environment is missing or package verification
fails, delegate installation with:

```text
<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>
```

Do not inline package-manager installation commands from this skill.

## Outputs

- `{output_dir}/objects/seurat_annotated.rds`
- `{output_dir}/tables/celltype_annotation.tsv`
- `{output_dir}/plots/umap_celltypes.png` when UMAP exists

## Rules

- Default to marker-first logic.
- Only use `SingleR` when the manifest explicitly supplies a reference.
- If neither marker rules nor a reference are supplied, label clusters as
  unresolved placeholders rather than inventing biological cell types.
