---
name: conserved
description: 在条件、样本或批次分组之间鉴定每个 cluster/cell type 的 conserved marker。Use after clustering or annotation when the
  user asks for conserved markers across groups; do not use for ordinary cluster marker discovery.
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

# scRNA Conserved Markers

Use this skill for the tutorial's advanced marker step: conserved marker
identification across groups.

## Required Inputs

- Manifest YAML
- `{output_dir}/objects/seurat_clustered.rds` or annotated Seurat RDS
- A grouping column such as `sample_id`, `condition`, or `batch`

## Optional Manifest Fields

- `conserved_markers.grouping_column`
- `conserved_markers.ident_column`
- `conserved_markers.idents`
- `conserved_markers.min_pct`
- `conserved_markers.logfc_threshold`
- `conserved_markers.only_pos`

## Workflow

1. Verify cluster/cell-type identities and grouping column.
2. Run conserved marker testing per requested cluster/cell type.
3. Export per-identity marker tables and a combined summary.
4. Keep conserved markers separate from ordinary cluster markers.

## Outputs

- `{output_dir}/tables/conserved_markers.tsv`
- `{output_dir}/tables/conserved_marker_summary.tsv`
- `{output_dir}/plots/conserved_marker_dotplot.png` when enough markers exist

## Ask User Before Running

Ask the user when any of these are unclear:

- Which column defines the groups that markers must be conserved across.
- Whether identities should be clusters or annotated cell types.
- Which clusters/cell types should be tested.

