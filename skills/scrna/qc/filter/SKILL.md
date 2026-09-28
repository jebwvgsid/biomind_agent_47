---
name: filter
description: 对 Seurat 单细胞对象执行质控统计、percent.mt 计算、阈值过滤和 QC 图导出。用于课程第4天的质控过滤步骤，也适用于其他样本的可重复 QC。Use when Codex needs
  a reusable QC step for scRNA-seq data before downstream normalization and clustering.
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

# scRNA QC Filter

Use this skill to run a manifest-driven QC step on an existing Seurat object.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_raw.rds` produced by the ingest step, or
- explicit `--input-rds` path

## Optional Manifest QC Fields

- `mt_pattern`
- `qc.min_features`
- `qc.max_features`
- `qc.min_counts`
- `qc.max_counts`
- `qc.max_percent_mt`

## Quick Start

```bash
Rscript "${SKILL_DIR}/scripts/qc_filter_seurat.R" --manifest /abs/path/manifest.yaml
```

## Environment

Use an existing environment containing the dependencies listed in `references/dependencies.md`. If dependencies are missing, ask the user before changing an environment:

```text
先确认所需依赖（scrna-env-bootstrap_r）与目标环境，再请求用户明确安装授权
```

Do not inline package-manager installation commands from this skill.

## Outputs

- `{output_dir}/objects/seurat_qc.rds`
- `{output_dir}/tables/qc_summary.tsv`
- `{output_dir}/tables/qc_metrics.tsv`
- `{output_dir}/plots/qc_violin.png`
- `{output_dir}/plots/qc_scatter.png`

## Rules

- Compute `percent.mt` if it does not already exist.
- Use manifest thresholds instead of hardcoding values in the script call.
- Preserve metadata columns and append QC metrics rather than dropping them.
- Export `qc_metrics.tsv` for all cells, not only retained cells, and include
  `pass_qc`, per-threshold pass flags, and threshold values for auditability.
