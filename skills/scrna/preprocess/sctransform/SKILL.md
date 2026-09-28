---
name: sctransform
description: 对 QC 后的 Seurat 对象执行 SCTransform 标准化、高变基因建模和可选协变量回归。Use when the user explicitly requests SCTransform/SCT
  or a workflow stage selects SCT preprocessing after scRNA QC; do not use as the default log-normalization path
  unless requested or justified by the workflow.
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

# scRNA SCTransform Preprocess

Use this skill for the tutorial's SCTransform preprocessing branch. It reuses
the existing preprocessing script with `preprocess.method: sctransform`.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_qc.rds`, or explicit `--input-rds`

## Required Manifest Fields

The manifest must set:

```yaml
preprocess:
  method: sctransform
```

Recommended fields:

- `preprocess.nfeatures`
- `preprocess.regress_vars`

## Quick Start

```bash
Rscript "${PREPROCESS_SKILL_DIR}/scripts/preprocess_scrna_seurat.R" --manifest /abs/path/manifest.yaml --input-rds /abs/path/seurat_qc.rds --output-rds /abs/path/objects/seurat_sct.rds
```

## Environment

Reuse an existing environment containing the declared scRNA dependencies. If `sctransform` is missing, delegate installation with:

```text
先确认所需依赖（scrna-env-bootstrap_r）与目标环境，再请求用户明确安装授权
```

Do not inline package-manager installation commands from this skill.

## Outputs

- `{output_dir}/objects/seurat_sct.rds` when `--output-rds` is supplied
- `{output_dir}/tables/variable_features.tsv`
- `{output_dir}/tables/preprocess_summary.tsv`
- `{output_dir}/plots/variable_features.png`

## Ask User Before Running

Ask the user when any of these are unclear:

- Whether SCT or classic log-normalization should be used.
- Which covariates should be regressed, especially `percent.mt`, cell-cycle
  scores, sample/batch columns, or chemistry/library columns.
- Whether batch correction should happen before or after SCT in a multi-sample
  project.

Never infer biological covariates from column names alone when the project
background is ambiguous.

