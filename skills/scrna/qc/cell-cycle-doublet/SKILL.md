---
name: cell-cycle-doublet
description: 对 Seurat 对象进行细胞周期评分、可选细胞周期回归决策和 doublet 检测前检查。Use after initial scRNA QC when the project needs cell-cycle
  assessment or doublet flagging; do not silently remove cells or regress covariates without user approval.
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

# scRNA Cell Cycle And Doublet QC

Use this skill to extend the tutorial's QC step with cell-cycle scoring and
doublet-aware review.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_qc.rds`, or explicit QC-filtered Seurat RDS
- Species or explicit S/G2M gene lists for cell-cycle scoring

## Optional Manifest Fields

- `cell_cycle.s_genes`
- `cell_cycle.g2m_genes`
- `cell_cycle.regress_after_scoring`
- `doublet.method`
  - `none`
  - `scrublet`
  - `scDblFinder`
  - `DoubletFinder`
- `doublet.expected_rate`

## Workflow

1. Verify the Seurat object and metadata columns.
2. Score cell cycle only when species or explicit gene lists are available.
3. Report phase composition by sample/cluster when those columns exist.
4. Run doublet detection only when a declared method and package are available.
5. Save flags and summaries; do not remove cells unless the user explicitly
   requested filtering.

## Ask User Before Running

Ask the user when any of these are unclear:

- Species or whether the object uses human/mouse/plant/custom gene symbols.
- Whether cell-cycle signal is expected biology or unwanted technical variation.
- Which doublet method should be used and expected doublet rate.
- Whether flagged cells should be removed or only annotated.

If a doublet package is missing, check this skill first for the declared method
and then delegate environment installation rather than inventing ad-hoc install
commands.

