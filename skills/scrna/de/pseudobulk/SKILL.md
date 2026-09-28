---
name: pseudobulk
description: 将单细胞表达按 sample/condition/cell type 聚合后执行 replicate-aware pseudo-bulk 差异表达。Use for formal condition
  contrasts with biological replicates; do not replace pseudo-bulk with per-cell DE when replicates are available.
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

# scRNA Pseudobulk DE

Use this skill for replicate-aware differential expression after annotation.
Pseudo-bulk is preferred over per-cell tests when a formal condition comparison
has biological replicates.

## Required Inputs

- Manifest YAML
- Annotated Seurat RDS
- Metadata columns for sample/replicate, condition, and optional cell type
- Explicit contrast direction

## Optional Manifest Fields

- `pseudobulk.sample_column`
- `pseudobulk.condition_column`
- `pseudobulk.celltype_column`
- `pseudobulk.target`
- `pseudobulk.control`
- `pseudobulk.method`
  - `edgeR`
  - `DESeq2`
  - `limma_voom`
- `pseudobulk.min_cells_per_sample`
- `pseudobulk.min_replicates_per_group`

## Workflow

1. Verify sample-level replicates and condition labels.
2. Ask for target/control direction if not explicit.
3. Aggregate counts by sample and, when requested, by cell type.
4. Run the declared pseudo-bulk method only if its package is available.
5. Export counts, design table, DE results, and a reproducible report.

## Outputs

- `{output_dir}/tables/pseudobulk_counts.tsv`
- `{output_dir}/tables/pseudobulk_design.tsv`
- `{output_dir}/tables/pseudobulk_de_results.tsv`
- `{output_dir}/plots/pseudobulk_de_volcano.png`
- `{output_dir}/report_pseudobulk_de.md`

## Ask User Before Running

Ask the user when any of these are unclear:

- Which column is biological replicate/sample.
- Which column is condition.
- Which condition is target and which is control.
- Whether DE should be run globally or within each annotated cell type.
- Whether enough biological replicates exist for the requested method.

If no replicate metadata exists, do not silently run cell-level DE; explain the
limitation and ask whether the user wants exploratory per-cell DE instead.

