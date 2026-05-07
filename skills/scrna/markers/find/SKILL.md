---
name: scRNA Markers Find
description: 对聚类后的 Seurat 对象执行 cluster marker 鉴定并导出 marker 表与概览图。Use when Codex needs a manifest-driven FindAllMarkers step after scRNA clustering.
tags: [single-cell, scRNA, Seurat, markers, differential-expression, 单细胞, marker, 标记基因]
legacy_ids: [scrna-markers-find]
runtime:
  default_env: bio_scrna_core
  env_runner: conda run -n bio_scrna_core Rscript
  language: r
  packages: [Seurat, yaml, ggplot2, dplyr]
  capabilities: [scrna-markers, scrna-cluster-markers]
---

# scRNA Markers Find

Use this skill to compute cluster markers from a clustered Seurat object.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_clustered.rds`, or explicit `--input-rds`

## Optional Manifest Fields

- `markers.only_pos`
- `markers.min_pct`
- `markers.logfc_threshold`
- `markers.test_use`
- `markers.top_n`

## Quick Start

```bash
conda run -n bio_scrna_core Rscript skills/scrna/markers/find/scripts/find_markers_seurat.R --manifest /abs/path/manifest.yaml
```

## Outputs

- `{output_dir}/tables/cluster_markers.tsv`
- `{output_dir}/tables/marker_summary.tsv`
- `{output_dir}/plots/marker_dotplot.png` when markers are available

## Rules

- Expect `seurat_clusters` to exist on the input object.
- Write an empty marker table instead of failing when the dataset only yields
  one cluster or no significant markers.
- Keep marker detection separate from cell annotation.
