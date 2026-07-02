---
name: scRNA Conserved Markers
description: 在条件、样本或批次分组之间鉴定每个 cluster/cell type 的 conserved marker。Use after clustering or annotation when the user asks for conserved markers across groups; do not use for ordinary cluster marker discovery.
tags: [single-cell, scRNA, Seurat, conserved-markers, marker, 单细胞]
task_types: [single_cell_rnaseq]
stage_tags: [conserved_marker_find]
inputs: [clustered_seurat_object, annotated_seurat_object, grouping_column]
outputs: [conserved_marker_table, conserved_marker_summary]
when_to_use:
  - User asks for conserved marker genes.
  - Workflow stage is conserved_marker_find.
when_not_to_use:
  - User only needs FindAllMarkers for clusters.
  - The grouping column for conserved testing is missing or ambiguous.
legacy_ids: [scrna-conserved-markers, scrna-markers-conserved]
runtime:
  default_env: bio_scrna_core
  env_runner: conda run -n bio_scrna_core Rscript
  language: r
  packages: [Seurat, yaml, ggplot2, dplyr]
  capabilities: [scrna-conserved-markers, scrna-markers]
  install_tool: '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>'
---

# scRNA Conserved Markers

Use this skill for the tutorial's advanced marker step: conserved marker
identification across groups.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_clustered.rds` or annotated Seurat RDS
- A grouping column such as `sample_id`, `condition`, or `batch`

## Optional Manifest Fields

- `conserved_markers.grouping_column`
- `conserved_markers.ident_column`
- `conserved_markers.idents`
- `conserved_markers.min_pct`
- `conserved_markers.logfc_threshold`
- `conserved_markers.only_pos`

## Workflow

1. Verify cluster/cell-type identities and grouping column.
2. Run conserved marker testing per requested cluster/cell type.
3. Export per-identity marker tables and a combined summary.
4. Keep conserved markers separate from ordinary cluster markers.

## Outputs

- `{output_dir}/tables/conserved_markers.tsv`
- `{output_dir}/tables/conserved_marker_summary.tsv`
- `{output_dir}/plots/conserved_marker_dotplot.png` when enough markers exist

## Ask User Before Running

Ask the user when any of these are unclear:

- Which column defines the groups that markers must be conserved across.
- Whether identities should be clusters or annotated cell types.
- Which clusters/cell types should be tested.

