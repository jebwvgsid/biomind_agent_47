---
name: crop-rnaseq-de
description: Perform crop bulk RNA-seq differential expression and candidate gene screening from count/TPM matrices,
  metadata, formulas, contrasts, and annotations. Use for cultivar, tissue, treatment, stage, high/low trait expression
  comparisons; automatically switch to TPM fold-change screening when biological replicates are insufficient.
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

# Crop RNA-seq DE

Use this skill for crop bulk RNA-seq expression analysis:

```text
count matrix + metadata + contrast -> QC/PCA -> DESeq2/edgeR or TPM screen -> annotated candidate genes
```

## Trigger Conditions

- User has `gene_counts.tsv`, `gene_tpm.tsv`, metadata, and asks for DEG/candidate genes.
- Comparisons involve crop genotype, tissue, condition, treatment, stage, high-sugar vs low-sugar, or similar trait groups.

Do not use for FASTQ/BAM preprocessing; use `crop-rnaseq-pipeline`. Do not use for plotting an already final DE table only; use `crop-diff-visualizer`.

## Inputs

- `--counts gene_counts.tsv`
- `--tpm gene_tpm.tsv`
- `--metadata sample_metadata.tsv`
- `--contrast condition,high_sugar,low_sugar`
- `--formula "~ batch + condition"`
- `--annotation gene_function.tsv` or `--gff genome.gff3`

Recommended metadata columns: `sample_id,genotype,tissue,condition,replicate,batch,stage,location,year`.

## Decision Rules

- If every contrast group has at least two biological replicates, run replicate-aware DEG with DESeq2 or edgeR.
- If any contrast group has fewer than two biological replicates, do not run statistical DE. Use TPM+1 log2FC screening and state clearly that results are candidate screening, not strict DEG.
- Keep contrast direction explicit: `factor,treatment,control` means positive log2FC is up in `treatment`.

## Workflow

1. Validate sample IDs across matrices and metadata.
2. Inspect replicate counts for each contrast.
3. Run:
   - `Rscript "${SKILL_DIR}/scripts/run_deseq2.R" ...` for DESeq2.
   - `Rscript "${SKILL_DIR}/scripts/run_edger.R" ...` for edgeR.
   - `python "${SKILL_DIR}/scripts/run_tpm_fc_filter.py" ...` for no-replicate screening.
4. Merge crop gene annotations:
   `python "${SKILL_DIR}/scripts/merge_annotation_expression.py" --de de_results.full.tsv --annotation gene_function.tsv --output de_results.annotated.tsv`
5. Write a handoff command for `crop-diff-visualizer`.

## Outputs

- `report.md`, `result.json`
- `tables/qc_summary.tsv`
- `tables/normalized_counts.tsv`
- `tables/de_results.full.tsv`
- `tables/de_results.significant.tsv`
- `tables/de_results.annotated.tsv`
- `tables/no_replicate_fc_screen.tsv`
- `figures/pca.png`, `figures/sample_correlation_heatmap.png`, `figures/volcano.png`, `figures/ma_plot.png`, `figures/top_deg_heatmap.png`
- `handoff/crop_diff_visualizer.command.sh`
- `reproducibility/commands.sh`, `environment.yml`, `sessionInfo.txt`

## Gotchas

- Do not silently treat technical replicates as biological replicates.
- Use raw integer counts for DESeq2/edgeR, not TPM.
- Support gene annotation from eggNOG, KEGG, Pfam, SwissProt, NR, and GFF3 attributes.
- Homeolog group columns should be preserved if present.

## Tests

Validate DESeq2, edgeR, or TPM fold-change behavior with independent validation data or user-provided matrices; do not keep local example matrices inside this skill.
