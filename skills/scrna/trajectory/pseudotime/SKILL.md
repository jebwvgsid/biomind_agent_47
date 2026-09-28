---
name: pseudotime
description: 基于已聚类/注释的单细胞对象进行轨迹推断、伪时间排序和分支相关基因分析，支持 Monocle3、Slingshot 或 dynverse 思路。Use when the user asks for
  trajectory, pseudotime, lineage, differentiation path, or temporal ordering; never infer root/direction without
  project background.
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

# scRNA Trajectory Pseudotime

Use this skill for the tutorial's trajectory inference step. The tutorial
mentions Monocle and dynverse-style method selection; BioMind should treat
method choice and root direction as biological decisions, not code defaults.

## Required Inputs

- Clustered or annotated Seurat RDS
- Root cell population, starting time point, or explicit lineage direction
- Method choice or permission to recommend one after inspecting data

## Optional Manifest Fields

- `trajectory.method`
  - `monocle3`
  - `slingshot`
  - `dynverse`
- `trajectory.root_column`
- `trajectory.root_value`
- `trajectory.lineage_column`
- `trajectory.lineage_values`
- `trajectory.embedding`

## Workflow

1. Verify clustered/annotated object and embedding availability.
2. Confirm biological root and lineage direction.
3. Verify requested trajectory package/environment.
4. Run trajectory inference only after root/direction are clear.
5. Export pseudotime per cell, trajectory plots, and branch-associated genes.

## Outputs

- `{output_dir}/tables/pseudotime_cells.tsv`
- `{output_dir}/tables/trajectory_branch_genes.tsv`
- `{output_dir}/plots/trajectory_pseudotime.png`
- `{output_dir}/plots/trajectory_celltypes.png`
- `{output_dir}/report_trajectory.md`

## Ask User Before Running

Ask the user when any of these are unclear:

- Which cell type, cluster, or time point should be the trajectory root.
- What biological lineage or transition is being tested.
- Whether branches should be interpreted as differentiation, activation, cell
  cycle, batch, or another process.
- Which method is preferred when multiple packages are available.

Do not default to the smallest cluster, first factor level, or earliest-looking
label as root.

