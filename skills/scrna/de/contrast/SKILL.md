---
name: contrast
description: 在样本分组、条件分组或指定细胞类型子集上执行差异表达并导出结果表与火山图。Use when Codex needs a manifest-driven differential expression
  step after scRNA annotation or clustering.
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

# scRNA DE Contrast

Use this skill to compute one differential-expression contrast from a Seurat
object.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_annotated.rds`, or explicit `--input-rds`

## Optional Manifest Fields

- `de.group_by`
- `de.ident_1`
- `de.ident_2`
- `de.subset_column`
- `de.subset_value`
- `de.test_use`
- `de.min_pct`
- `de.logfc_threshold`
- `de.latent_vars`

## Quick Start

```bash
Rscript "${SKILL_DIR}/scripts/de_contrast_seurat.R" --manifest /abs/path/manifest.yaml
```

## Environment

Use an existing environment containing the dependencies listed in `references/dependencies.md`. If dependencies are missing, ask the user before changing an environment:

```text
先确认所需依赖（scrna-env-bootstrap_r）与目标环境，再请求用户明确安装授权
```

Do not inline package-manager installation commands from this skill.

## Outputs

- `{output_dir}/tables/de_results.tsv`
- `{output_dir}/tables/de_summary.tsv`
- `{output_dir}/plots/de_volcano.png` when results are available

## Rules

- Prefer `sample_id` as the default grouping column when exactly two groups are
  present.
- If no explicit labels are supplied and the grouping column has exactly two
  levels, use those two levels as the contrast automatically.
- Keep DE separate from cluster marker detection.
