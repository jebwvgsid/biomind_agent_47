---
name: scRNA Ingest QC Workflow
description: >
  Run the first scRNA-seq workflow stage: verify the Seurat runtime, ingest raw
  scRNA inputs into a Seurat object, then run QC filtering from the same manifest.
  Use when the user asks for scRNA ingest + QC, single-cell data preparation,
  Seurat object construction followed by QC, or a one-command wrapper for the
  existing env/bootstrap, ingest/data-download, and qc/filter skills.
tags: [single-cell, scRNA, Seurat, workflow, ingest, QC, 单细胞, 质控]
task_types: [single_cell_rnaseq]
stage_tags: [ingest_qc]
inputs: [manifest_yaml, raw_count_matrix, tenx_mtx, tenx_h5, seurat_rds]
outputs: [raw_seurat_object, qc_filtered_seurat_object, qc_report]
when_to_use:
  - Workflow stage is ingest_qc.
  - User wants a one-command wrapper for ingest plus QC.
when_not_to_use: [Bulk RNA-seq, downstream-only analysis after QC]
legacy_ids: [scrna-ingest-qc, scrna_ingest_qc, run_scrna_ingest_qc]
runtime:
  default_env: bio_scrna_core
  env_runner: bash
  language: bash
  packages: [Seurat, Matrix, yaml, ggplot2, patchwork]
  capabilities: [scrna-workflow, scrna-ingest, scrna-qc]
  install_tool: '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>'
  healthcheck: bash skills/scrna/workflows/ingest-qc/scripts/run_scrna_ingest_qc.sh --help
---

# scRNA Ingest QC Workflow

This workflow composes three existing scRNA skills:

1. `skills/scrna/env/bootstrap/scripts/check_scrna_env.R`
2. `skills/scrna/ingest/data-download/scripts/ingest_scrna_seurat.R`
3. `skills/scrna/qc/filter/scripts/qc_filter_seurat.R`

Use it when a manifest should go from raw input to a QC-filtered Seurat object in one command.

## Quick Start

```bash
bash skills/scrna/workflows/ingest-qc/scripts/run_scrna_ingest_qc.sh /abs/path/manifest.yaml
```

Optional environment override:

```bash
bash skills/scrna/workflows/ingest-qc/scripts/run_scrna_ingest_qc.sh /abs/path/manifest.yaml bio_scrna_core
```

## Inputs

- Manifest YAML accepted by `scrna/ingest/data-download`
- Optional conda environment name; defaults to `bio_scrna_core`

## Outputs

The workflow writes the standard ingest and QC outputs declared by the underlying skills:

- `{output_dir}/objects/seurat_raw.rds`
- `{output_dir}/tables/sample_manifest.tsv`
- `{output_dir}/tables/cell_counts.tsv`
- `{output_dir}/objects/seurat_qc.rds`
- `{output_dir}/tables/qc_summary.tsv`
- `{output_dir}/tables/qc_metrics.tsv`
- `{output_dir}/plots/qc_violin.png`
- `{output_dir}/plots/qc_scatter.png`

## Rules

- Keep this script as orchestration only; do not duplicate ingest or QC logic here.
- If environment verification fails, delegate installation with:
  `<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>`
- Modify the underlying skill scripts only when the individual step behavior needs to change.
