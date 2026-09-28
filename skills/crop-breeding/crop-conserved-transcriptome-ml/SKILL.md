---
name: crop-conserved-transcriptome-ml
description: 'Predict crop phenotypes from transcriptome expression using evolutionarily conserved response genes,
  ortholog mapping, XGBoost regression, feature importance, and optional GENIE3 regulatory-network prioritization.
  Use for conserved DEG-based transcriptome phenotype prediction, NUE-style gene-to-trait modeling, and candidate
  TF discovery; do not use for SNP-only genomic prediction.

  '
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

# Crop Conserved Transcriptome ML

Use this skill for transcriptome-based phenotype prediction with biology-guided feature reduction:

```text
expression + phenotype + condition metadata + ortholog table
-> response DEG selection
-> within- and cross-species conserved gene filtering
-> XGBoost phenotype prediction
-> feature importance
-> optional GENIE3 TF network
-> candidate gene report
```

## Source Alignment

Read `references/source-notes.md` before citing the Nature Communications NUE workflow. The original code is available through OSF, not GitHub.

## Environment And Data SOP

Read `references/install-and-data.md` before installing XGBoost/GENIE3, downloading OSF or GEO data, or claiming exact paper reproduction. Formal analysis must use XGBoost and, when TF prioritization is requested, GENIE3 or the upstream paper workflow.

## Inputs

- `expression_matrix.tsv`: rows are samples, columns are genes, values are normalized counts or expression.
- `phenotype.tsv`: `sample_id`, trait columns, genotype, species, and optional replicate columns.
- `condition_metadata.tsv`: sample condition, treatment, tissue, timepoint, genotype, and species.
- `species_ortholog_table.tsv`: ortholog or homolog mapping across species.
- `tf_list.tsv`: optional transcription factor list for GENIE3 regulator selection.

## Workflow

1. Verify that expression and phenotype come from matched or biologically comparable samples.
2. Identify treatment- or condition-responsive DEGs within each species or genotype group.
3. Intersect response genes across genotypes and then across species through the ortholog table.
4. Train XGBoost regression models with leave-genotype-out or grouped CV when genotype generalization is the goal.
5. Compare conserved-response features against random expressed genes and all expressed genes when feasible.
6. Rank important genes by XGBoost gain/cover/weight or SHAP-style attributions.
7. If TF candidates are requested, run or plan GENIE3 with response genes as targets and TF list as regulators.

## Formal Execution

Use the upstream OSF/GEO workflow or a recorded XGBoost + GENIE3 environment for exact execution. Do not substitute BioMind-local Python scripts for the paper's XGBoost/GENIE3 analysis.

## Outputs

- `conserved_response_genes.tsv`
- `xgboost_prediction.tsv`
- `model_performance.tsv`
- `important_genes.tsv`
- `tf_network_candidates.tsv`
- `candidate_gene_report.md`

## Rules

- Do not treat feature importance as proof of causality; present it as candidate prioritization.
- Keep leave-genotype-out and random-split results separate.
- Report whether features are conserved within species, across species, or both.
- Use `crop-rnaseq-de` when the task is only DEG detection; use this skill when DEG selection feeds phenotype prediction.

## Related Skills

- `crop-breeding/crop-rnaseq-de` - differential expression screening.
- `crop-breeding/crop-rnaseq-pipeline` - build expression matrices from sequencing data.
- `crop-breeding/crop-dnngp-multiomics-prediction` - broader omics and multi-omics model comparison.
- `crop-breeding/crop-smart-breeding-strategy` - choose transcriptome vs genotype prediction workflow.
