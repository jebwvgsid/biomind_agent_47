---
name: scRNA Cell Cycle And Doublet QC
description: 对 Seurat 对象进行细胞周期评分、可选细胞周期回归决策和 doublet 检测前检查。Use after initial scRNA QC when the project needs cell-cycle assessment or doublet flagging; do not silently remove cells or regress covariates without user approval.
tags: [single-cell, scRNA, Seurat, QC, cell-cycle, doublet, 单细胞, 细胞周期]
task_types: [single_cell_rnaseq]
stage_tags: [cell_cycle_doublet_qc]
inputs: [qc_filtered_seurat_object, species, cell_cycle_gene_sets]
outputs: [qc_annotated_seurat_object, cell_cycle_table, doublet_table]
when_to_use:
  - User asks for cell-cycle scoring, regression decision, doublet detection, or enhanced scRNA QC.
  - Workflow stage is cell_cycle_doublet_qc.
when_not_to_use:
  - The user only requested basic mitochondrial/count filtering.
  - Required species-specific gene sets or doublet packages are unavailable.
legacy_ids: [scrna-cell-cycle-doublet, scrna-enhanced-qc]
runtime:
  default_env: bio_scrna_core
  env_runner: conda run -n bio_scrna_core Rscript
  language: r
  packages: [Seurat, yaml, ggplot2, dplyr]
  capabilities: [scrna-cell-cycle, scrna-doublet-qc]
  install_tool: '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>'
---

# scRNA Cell Cycle And Doublet QC

Use this skill to extend the tutorial's QC step with cell-cycle scoring and
doublet-aware review.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_qc.rds`, or explicit QC-filtered Seurat RDS
- Species or explicit S/G2M gene lists for cell-cycle scoring

## Optional Manifest Fields

- `cell_cycle.s_genes`
- `cell_cycle.g2m_genes`
- `cell_cycle.regress_after_scoring`
- `doublet.method`
  - `none`
  - `scrublet`
  - `scDblFinder`
  - `DoubletFinder`
- `doublet.expected_rate`

## Workflow

1. Verify the Seurat object and metadata columns.
2. Score cell cycle only when species or explicit gene lists are available.
3. Report phase composition by sample/cluster when those columns exist.
4. Run doublet detection only when a declared method and package are available.
5. Save flags and summaries; do not remove cells unless the user explicitly
   requested filtering.

## Ask User Before Running

Ask the user when any of these are unclear:

- Species or whether the object uses human/mouse/plant/custom gene symbols.
- Whether cell-cycle signal is expected biology or unwanted technical variation.
- Which doublet method should be used and expected doublet rate.
- Whether flagged cells should be removed or only annotated.

If a doublet package is missing, check this skill first for the declared method
and then delegate environment installation rather than inventing ad-hoc install
commands.

