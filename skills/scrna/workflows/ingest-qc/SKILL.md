---
name: ingest-qc
description: 'Run the first scRNA-seq workflow stage: verify the Seurat runtime, ingest raw scRNA inputs into a
  Seurat object, then run QC filtering from the same manifest. Use when the user asks for scRNA ingest + QC, single-cell
  data preparation, Seurat object construction followed by QC, or a one-command wrapper for the existing env/bootstrap,
  ingest/data-download, and qc/filter skills.

  '
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

# scRNA Ingest QC Workflow

This workflow composes three existing scRNA skills:

1. `bootstrap` 技能 baseDir 下的 `scripts/check_scrna_env.R`
2. `data-download` 技能 baseDir 下的 `scripts/ingest_scrna_seurat.R`
3. `filter` 技能 baseDir 下的 `scripts/qc_filter_seurat.R`

Use it when a manifest should go from raw input to a QC-filtered Seurat object in one command.

## Quick Start

```bash
bash "${SKILL_DIR}/scripts/run_scrna_ingest_qc.sh" /abs/path/manifest.yaml
```

Optional environment override:

```bash
bash "${SKILL_DIR}/scripts/run_scrna_ingest_qc.sh" /abs/path/manifest.yaml bio_scrna_core
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
  `先确认所需依赖（scrna-env-bootstrap_r）与目标环境，再请求用户明确安装授权`
- Modify the underlying skill scripts only when the individual step behavior needs to change.
