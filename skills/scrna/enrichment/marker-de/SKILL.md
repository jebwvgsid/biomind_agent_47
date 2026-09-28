---
name: marker-de
description: 从单细胞 marker 或 DE 结果构建前景/背景/排序基因集，并交给通用 enrichment-analysis skill 执行 GO/KEGG/GSEA/TERM2GENE 富集。Use after
  scRNA marker discovery or DE; do not perform DE or marker testing itself.
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

# scRNA Marker And DE Enrichment

Use this skill as the bridge between scRNA marker/DE tables and the general
`enrichment-analysis` skill.

## Required Inputs

- Marker table or DE table
- Species and gene ID type
- Background gene universe
- Annotation database, annotation table, or TERM2GENE
- Output directory

## Input Interpretation

- Cluster marker table: build one gene set per cluster/cell type.
- DE table: split target-up and control-up genes before enrichment.
- Ranked DE table: build a ranked list for GSEA when most tested genes are
  present with signed statistics.

## Workflow

1. Verify the marker/DE table columns.
2. Decide ORA vs GSEA from available input:
   - significant gene list -> ORA
   - ranked statistic for most tested genes -> GSEA
3. Build foreground gene sets per cluster/cell type/contrast direction.
4. Use the detected/tested genes from the Seurat object or marker/DE universe
   as the background.
5. Select and run `enrichment-analysis` with species-aware database rules.

## Related Skill

- `enrichment-analysis` - required for actual enrichment statistics and plots.

## Ask User Before Running

Ask the user when any of these are unclear:

- Species, gene ID type, or annotation source.
- Whether enrichment should be per cluster, per annotated cell type, or per
  contrast direction.
- Which table columns represent gene ID, adjusted p-value, logFC, statistic,
  cluster, and cell type.
- What background gene universe should be used.

Never use the whole genome as a default background for scRNA unless the user
explicitly requests it or the experiment genuinely tested the whole genome.

