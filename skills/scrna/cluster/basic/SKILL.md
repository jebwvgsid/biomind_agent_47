---
name: scRNA Cluster Basic
description: 对预处理后的 Seurat 对象执行 PCA、邻近图、聚类、UMAP 和可选 t-SNE，并保存聚类后的稳定对象。Use when Codex needs a manifest-driven clustering step after scRNA preprocessing.
tags: [single-cell, scRNA, Seurat, PCA, UMAP, clustering, 单细胞, 聚类, 降维]
legacy_ids: [scrna-cluster-basic]
runtime:
  default_env: bio_scrna_core
  env_runner: conda run -n bio_scrna_core Rscript
  language: r
  packages: [Seurat, yaml, ggplot2, patchwork, dplyr]
  capabilities: [scrna-cluster, scrna-pca, scrna-umap]
---

# scRNA Cluster Basic

Use this skill to generate PCA, UMAP, clustering, and cluster-level summary
artifacts from a normalized Seurat object.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_norm.rds`, or explicit `--input-rds`

## Optional Manifest Fields

- `cluster.pca_npcs`
- `cluster.cluster_dims`
- `cluster.resolution`
- `cluster.run_tsne`

## Quick Start

```bash
conda run -n bio_scrna_core Rscript skills/scrna/cluster/basic/scripts/cluster_scrna_seurat.R --manifest /abs/path/manifest.yaml
```

## Outputs

- `{output_dir}/objects/seurat_clustered.rds`
- `{output_dir}/tables/cluster_sizes.tsv`
- `{output_dir}/tables/cluster_summary.tsv`
- `{output_dir}/plots/pca_elbow.png`
- `{output_dir}/plots/umap_clusters.png`
- `{output_dir}/plots/umap_sample.png`
- `{output_dir}/plots/tsne_clusters.png` when t-SNE runs

## Rules

- Keep a single clustered checkpoint in `seurat_clustered.rds`.
- Default to `pca_npcs=30`, `cluster_dims=1:20`, `resolution=0.6`.
- Always emit `umap_sample.png` when sample metadata is available so batch
  structure remains visible.
