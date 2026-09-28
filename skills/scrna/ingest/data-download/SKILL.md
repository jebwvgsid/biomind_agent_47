---
name: data-download
description: 读取单细胞转录组输入并构建 Seurat 对象，支持 10x mtx、10x h5、已有 Seurat RDS，以及用于 smoke test 的 builtin demo。用于课程第4天的数据准备、公共数据接入和
  Seurat 对象构建。Use when Codex needs a reusable data-ingest step for scRNA-seq samples.
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
Rscript "${SKILL_DIR}/scripts/ingest_scrna_seurat.R" --manifest /abs/path/manifest.yaml
```

## Environment

Use an existing environment containing the dependencies listed in `references/dependencies.md`. If dependencies are missing, ask the user before changing an environment:

```text
先确认所需依赖（scrna-env-bootstrap_r）与目标环境，再请求用户明确安装授权
```

Do not inline package-manager installation commands from this skill.

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
