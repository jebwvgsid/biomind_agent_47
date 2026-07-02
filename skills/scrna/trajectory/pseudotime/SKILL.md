---
name: scRNA Trajectory Pseudotime
description: 基于已聚类/注释的单细胞对象进行轨迹推断、伪时间排序和分支相关基因分析，支持 Monocle3、Slingshot 或 dynverse 思路。Use when the user asks for trajectory, pseudotime, lineage, differentiation path, or temporal ordering; never infer root/direction without project background.
tags: [single-cell, scRNA, trajectory, pseudotime, Monocle3, Slingshot, dynverse, 单细胞, 轨迹推断]
task_types: [single_cell_rnaseq]
stage_tags: [trajectory_pseudotime]
inputs: [clustered_seurat_object, annotated_seurat_object, root_cells, lineage_definition]
outputs: [pseudotime_table, trajectory_plots, branch_gene_table]
when_to_use:
  - User asks for trajectory inference, pseudotime, differentiation trajectory, lineage, branch analysis, Monocle, Slingshot, or dynverse.
  - Workflow stage is trajectory_pseudotime.
when_not_to_use:
  - No biological direction, root cell, time point, or lineage hypothesis is available.
  - The requested method is unavailable and no approved environment repair is possible.
legacy_ids: [scrna-pseudotime, scrna-trajectory]
runtime:
  default_env: bio_scrna_core
  env_runner: conda run -n bio_scrna_core Rscript
  language: r
  packages: [Seurat, yaml, ggplot2, dplyr]
  capabilities: [scrna-trajectory, scrna-pseudotime]
  install_tool: '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>'
---

# scRNA Trajectory Pseudotime

Use this skill for the tutorial's trajectory inference step. The tutorial
mentions Monocle and dynverse-style method selection; BioMind should treat
method choice and root direction as biological decisions, not code defaults.

## Required Inputs

- Clustered or annotated Seurat RDS
- Root cell population, starting time point, or explicit lineage direction
- Method choice or permission to recommend one after inspecting data

## Optional Manifest Fields

- `trajectory.method`
  - `monocle3`
  - `slingshot`
  - `dynverse`
- `trajectory.root_column`
- `trajectory.root_value`
- `trajectory.lineage_column`
- `trajectory.lineage_values`
- `trajectory.embedding`

## Workflow

1. Verify clustered/annotated object and embedding availability.
2. Confirm biological root and lineage direction.
3. Verify requested trajectory package/environment.
4. Run trajectory inference only after root/direction are clear.
5. Export pseudotime per cell, trajectory plots, and branch-associated genes.

## Outputs

- `{output_dir}/tables/pseudotime_cells.tsv`
- `{output_dir}/tables/trajectory_branch_genes.tsv`
- `{output_dir}/plots/trajectory_pseudotime.png`
- `{output_dir}/plots/trajectory_celltypes.png`
- `{output_dir}/report_trajectory.md`

## Ask User Before Running

Ask the user when any of these are unclear:

- Which cell type, cluster, or time point should be the trajectory root.
- What biological lineage or transition is being tested.
- Whether branches should be interpreted as differentiation, activation, cell
  cycle, batch, or another process.
- Which method is preferred when multiple packages are available.

Do not default to the smallest cluster, first factor level, or earliest-looking
label as root.

