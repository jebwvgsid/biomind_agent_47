---
name: scrna-cluster-basic
description: 对预处理后的 Seurat 对象执行 PCA、邻近图、聚类、UMAP 和可选 t-SNE，并保存聚类后的稳定对象。Use when Codex needs a manifest-driven clustering
  step after scRNA preprocessing.
compatibility: 需要本技能 references/dependencies.md 列明的现有软件；不自动安装。输入输出位于分析项目。
---


## 资源定位与依赖

使用当前 Agent 的标准技能发现与文件读取能力加载本文件。`scripts/`、`references/`、
`assets/` 相对本 SKILL.md 的实际目录；输入、输出和样本清单相对本次分析项目。
命令中的 `${SKILL_DIR}` 必须先设为实际技能目录的绝对路径；该变量不由宿主自动提供。
例如，从已加载位置解析目录后，以带引号的脚本绝对路径执行，保持工作目录为分析项目，
不要在技能安装目录写结果。跨技能资源按其实际目录定位；分享完整技能包时保留目录结构。
软件和参考数据库来自已有环境或用户显式提供的位置。依赖见
[dependencies.md](references/dependencies.md)；安装、升级或修改环境需要事先明确授权。
本文命令可由任意提供命令执行能力的 Agent 或终端使用；不会在读取技能时安装或体检环境。

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
Rscript "${SKILL_DIR}/scripts/cluster_scrna_seurat.R" --manifest /abs/path/manifest.yaml
```

## Environment

Use an existing environment containing the dependencies listed in `references/dependencies.md`. If dependencies are missing, ask the user before changing an environment:

```text
先确认所需依赖（scrna-env-bootstrap_r）与目标环境，再请求用户明确安装授权
```

Do not inline package-manager installation commands from this skill.

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
