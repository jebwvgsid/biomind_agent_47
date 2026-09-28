---
name: scrna-preprocess-basic
description: 对 QC 后的 Seurat 对象执行标准化、高变基因筛选和缩放，作为下游 PCA、UMAP 和聚类的统一预处理步骤。Use when Codex needs a manifest-driven preprocessing
  step after scRNA QC.
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

# scRNA Preprocess Basic

Use this skill to normalize a QC-filtered Seurat object and save a stable
checkpoint for downstream clustering.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_qc.rds`, or explicit `--input-rds`

## Optional Manifest Fields

- `preprocess.method`
  - `lognorm` (default)
  - `sctransform`
- `preprocess.nfeatures`
- `preprocess.scale_features`
  - `variable` (default)
  - `all`
- `preprocess.regress_vars`

## Quick Start

```bash
Rscript "${SKILL_DIR}/scripts/preprocess_scrna_seurat.R" --manifest /abs/path/manifest.yaml
```

## Environment

Use an existing environment containing the dependencies listed in `references/dependencies.md`. If dependencies are missing, ask the user before changing an environment:

```text
先确认所需依赖（scrna-env-bootstrap_r）与目标环境，再请求用户明确安装授权
```

Do not inline package-manager installation commands from this skill.

## Outputs

- `{output_dir}/objects/seurat_norm.rds`
- `{output_dir}/tables/variable_features.tsv`
- `{output_dir}/tables/preprocess_summary.tsv`
- `{output_dir}/plots/variable_features.png`

## Rules

- Reuse the same manifest used for ingest and QC.
- Default to Seurat classic preprocessing:
  `NormalizeData -> FindVariableFeatures -> ScaleData`.
- Only use `SCTransform` when the manifest explicitly requests it.
- Keep checkpoint names stable; do not create extra PCA-only RDS files.
