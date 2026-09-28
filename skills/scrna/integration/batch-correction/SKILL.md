---
name: batch-correction
description: 对多样本 scRNA Seurat 对象执行批次整合或批次效应校正，支持 Seurat anchor/RPCA/CCA 或 Harmony 决策。Use when a project has multiple
  samples/batches and needs integrated embeddings before clustering or annotation; do not integrate when batch and
  biological condition are confounded without asking the user.
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

# scRNA Integration Batch Correction

Use this skill for multi-sample scRNA integration before clustering,
annotation, or cross-condition comparison.

## Required Inputs

- Manifest YAML
- QC-filtered or preprocessed Seurat RDS
- Metadata column identifying sample/batch

## Optional Manifest Fields

- `integration.method`
  - `seurat_anchors`
  - `rpca`
  - `cca`
  - `harmony`
- `integration.batch_column`
- `integration.condition_column`
- `integration.dims`
- `integration.nfeatures`

## Workflow

1. Verify sample/batch metadata and whether there are at least two groups.
2. Inspect whether batch and biological condition are confounded.
3. Choose method only from explicit user request or manifest.
4. Generate integrated object and before/after embedding QC.
5. Preserve raw RNA assay and original metadata for downstream DE.

## Outputs

- `{output_dir}/objects/seurat_integrated.rds`
- `{output_dir}/tables/integration_summary.tsv`
- `{output_dir}/plots/umap_before_integration.png`
- `{output_dir}/plots/umap_after_integration.png`

## Ask User Before Running

Ask the user when any of these are unclear:

- Which metadata column is batch/sample/library.
- Which metadata column is the biological condition.
- Whether to integrate all samples together or within tissue/cell-type subsets.
- Whether batch correction is appropriate when condition and batch are
  confounded.

Do not treat every `orig.ident` difference as a removable batch effect.

