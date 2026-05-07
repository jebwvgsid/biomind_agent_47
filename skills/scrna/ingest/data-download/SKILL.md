---
name: scRNA Data Download And Ingest
description: 读取单细胞转录组输入并构建 Seurat 对象，支持 10x mtx、10x h5、已有 Seurat RDS，以及用于 smoke test 的 builtin demo。用于课程第4天的数据准备、公共数据接入和 Seurat 对象构建。Use when Codex needs a reusable data-ingest step for scRNA-seq samples.
tags: [single-cell, scRNA, Seurat, 10x, ingest, 单细胞, 数据导入]
legacy_ids: [scrna-data-download-ingest]
runtime:
  default_env: bio_scrna_core
  env_runner: conda run -n bio_scrna_core Rscript
  language: r
  packages: [Seurat, Matrix, yaml]
  capabilities: [scrna-ingest, scrna-seurat-object]
  install_command: conda create -n bio_scrna_core -c conda-forge -c bioconda r-base=4.3 r-seurat r-matrix r-yaml -y
---

# scRNA Data Download And Ingest

Use this skill to turn raw scRNA inputs into a reusable Seurat object.

## Supported Input Modes

- `10x_mtx`
- `10x_h5`
- `seurat_rds`
- `public_demo`

## Required Manifest Fields

- `project_name`
- `input_mode`
- `output_dir`

Optional but recommended:

- `species`
- `sample_id`
- `samples`
- `demo_name`

## Manifest Conventions

- `samples` should be a list of sample entries with `sample_id` and `path`.
- If `samples` is missing, fall back to top-level `input_path` and `sample_id`.
- `public_demo` currently supports `pbmc_small_builtin` for smoke tests.

## Quick Start

```bash
conda run -n bio_scrna_core Rscript skills/scrna/ingest/data-download/scripts/ingest_scrna_seurat.R --manifest /abs/path/manifest.yaml
```

## Outputs

- `{output_dir}/objects/seurat_raw.rds`
- `{output_dir}/tables/sample_manifest.tsv`
- `{output_dir}/tables/cell_counts.tsv`

## Rules

- Do not hardcode sample names or local paths.
- Use the manifest as the single source of truth.
- When multiple samples are provided, merge them into one Seurat object and
  preserve sample identity in metadata.
- For `input_mode: seurat_rds`, preserve existing `orig.ident` and condition
  metadata; only fill `sample_id` or `orig.ident` when they are missing.
