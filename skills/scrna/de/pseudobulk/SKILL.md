---
name: scRNA Pseudobulk DE
description: 将单细胞表达按 sample/condition/cell type 聚合后执行 replicate-aware pseudo-bulk 差异表达。Use for formal condition contrasts with biological replicates; do not replace pseudo-bulk with per-cell DE when replicates are available.
tags: [single-cell, scRNA, pseudobulk, differential-expression, DESeq2, edgeR, 单细胞, 差异表达]
task_types: [single_cell_rnaseq]
stage_tags: [pseudobulk_de]
inputs: [annotated_seurat_object, sample_metadata, contrast_definition]
outputs: [pseudobulk_counts, pseudobulk_de_table, pseudobulk_de_report]
when_to_use:
  - User asks for formal DE between conditions with biological replicates.
  - Workflow stage is pseudobulk_de.
when_not_to_use:
  - No sample/replicate metadata is available.
  - Target/control direction or contrast is unclear.
legacy_ids: [scrna-pseudobulk-de, scrna-de-pseudobulk]
runtime:
  default_env: bio_scrna_core
  env_runner: conda run -n bio_scrna_core Rscript
  language: r
  packages: [Seurat, yaml, dplyr]
  capabilities: [scrna-pseudobulk, scrna-de]
  install_tool: '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>'
---

# scRNA Pseudobulk DE

Use this skill for replicate-aware differential expression after annotation.
Pseudo-bulk is preferred over per-cell tests when a formal condition comparison
has biological replicates.

## Required Inputs

- Manifest YAML
- Annotated Seurat RDS
- Metadata columns for sample/replicate, condition, and optional cell type
- Explicit contrast direction

## Optional Manifest Fields

- `pseudobulk.sample_column`
- `pseudobulk.condition_column`
- `pseudobulk.celltype_column`
- `pseudobulk.target`
- `pseudobulk.control`
- `pseudobulk.method`
  - `edgeR`
  - `DESeq2`
  - `limma_voom`
- `pseudobulk.min_cells_per_sample`
- `pseudobulk.min_replicates_per_group`

## Workflow

1. Verify sample-level replicates and condition labels.
2. Ask for target/control direction if not explicit.
3. Aggregate counts by sample and, when requested, by cell type.
4. Run the declared pseudo-bulk method only if its package is available.
5. Export counts, design table, DE results, and a reproducible report.

## Outputs

- `{output_dir}/tables/pseudobulk_counts.tsv`
- `{output_dir}/tables/pseudobulk_design.tsv`
- `{output_dir}/tables/pseudobulk_de_results.tsv`
- `{output_dir}/plots/pseudobulk_de_volcano.png`
- `{output_dir}/report_pseudobulk_de.md`

## Ask User Before Running

Ask the user when any of these are unclear:

- Which column is biological replicate/sample.
- Which column is condition.
- Which condition is target and which is control.
- Whether DE should be run globally or within each annotated cell type.
- Whether enough biological replicates exist for the requested method.

If no replicate metadata exists, do not silently run cell-level DE; explain the
limitation and ask whether the user wants exploratory per-cell DE instead.

