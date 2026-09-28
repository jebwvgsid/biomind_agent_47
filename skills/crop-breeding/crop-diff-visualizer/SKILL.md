---
name: crop-diff-visualizer
description: Visualize and summarize crop differential expression or marker tables with volcano plots, MA plots,
  heatmaps, top DEG bars, candidate labels, and Markdown/HTML reports. Use after crop-rnaseq-de or when the user
  already has DE/marker tables; do not recompute differential expression.
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

# Crop Diff Visualizer

Use this skill only for downstream visualization and candidate reporting:

```text
DE table + TPM + metadata + candidate list -> figures + highlighted gene tables + report
```

## Inputs

- `--de-table de_results.annotated.tsv`
- `--tpm-matrix gene_tpm.tsv`
- `--metadata sample_metadata.tsv`
- `--candidate-gene-list candidate_genes.txt`
- `--highlight-keywords sucrose,photosynthesis,transporter,starch,kinase`
- Optional sample color mapping for labels such as `18l,18s,175l,175s`.

## Workflow

1. Auto-detect columns: `gene_id`, `log2FoldChange`, `pvalue`, `padj`, `baseMean`, `mean_tpm`, `annotation`.
2. Detect no-replicate screening tables; use `abs_log2FC` and TPM thresholds without calling padj-based significance strict DEG.
3. Generate volcano, MA plot, top up/down barplot, heatmap, and candidate gene expression plot.
4. Highlight genes matching candidate list or crop trait keywords.
5. Write `report.md`, `report.html`, and `result.json`.

## Outputs

- `figures/volcano.labelled.png`
- `figures/ma_plot.png`
- `figures/top_up_down_genes.barplot.png`
- `figures/top_deg_heatmap.png`
- `figures/sample_correlation.png`
- `figures/candidate_gene_expression.png`
- `tables/top_genes.tsv`
- `tables/significant_genes.tsv`
- `tables/highlighted_candidate_genes.tsv`
- `tables/figure_gene_labels.tsv`

## Gotchas

- Do not rerun DESeq2, edgeR, or fold-change analysis here.
- When no-replicate mode is detected, label outputs as screening candidates.
- Prefer functional keyword labels for crop biology: sugar, sucrose, starch, photosynthesis, transporter, kinase, disease, flowering, yield.

## Validation Boundary

Do not keep local example matrices inside this skill. Validate report rendering with independent validation data or user-provided analysis outputs.
