---
name: crop-dnngp-multiomics-prediction
description: 'Train, compare, or plan DNNGP-style plant genomic prediction from SNP, transcriptome, metabolome,
  epigenome, or fused multi-omics features using normalization, deep neural networks, batch normalization, ReLU,
  dropout, early stopping, and baseline model comparison. Use for crop multi-omics phenotype prediction and genomic
  selection; do not use for GWAS-weighted Transformer models.

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

# Crop DNNGP Multiomics Prediction

Use this skill to compare SNP-only, single-omics, and multi-omics feature sets for crop phenotype prediction:

```text
SNP / transcriptome / metabolome / epigenome matrices
-> sample alignment and normalization
-> baseline models
-> DNNGP-style deep neural network
-> prediction and feature importance
```

## Source Alignment

Read `references/source-notes.md` before using paper-specific DNNGP settings. The upstream DNNGP project is TensorFlow-based and provides manuals, scripts, example input/output folders, and batch hyperparameter tuning.

## Environment And Data SOP

Read `references/install-and-data.md` before installing DNNGP environments, downloading example files, or claiming exact TensorFlow reproduction. For formal biological conclusions, run the upstream TensorFlow DNNGP implementation and record the exact software version.

## Modes

- `snp-only`: train and evaluate only marker features.
- `single-omics-comparison`: train one model per omics matrix and compare predictive value.
- `multi-omics-fusion`: concatenate or late-fuse multiple omics matrices after strict sample alignment.

## Inputs

- `phenotype.tsv`: `sample_id`, trait columns, optional environment or batch columns.
- `genotype_matrix.tsv`: optional SNP or marker matrix.
- `expression_matrix.tsv`, `metabolite_matrix.tsv`, `methylation_matrix.tsv`, or other omics matrices.
- `feature_annotation.tsv`: optional feature-to-gene/metabolite annotation.
- `covariates.tsv`: optional population structure, family, batch, year, or environment covariates.

## Task Scope And Shared Checks

Read `../_shared/gp-audit-and-data-validation.md` sections A–C for task evidence, matrix identity and observation checks. Apply the checks relevant to the requested analysis; complete a requested readiness audit even when a later training stage lacks inputs or resources. Keep design declarations, actual execution and stage-specific blockers separate.

## Workflow

1. Apply the shared matrix/ID checks to every omics block; output inclusion/exclusion and feature-accounting tables, preserving unlabeled prediction targets separately.
2. Define the prediction target and train/tune/test or outer/inner CV members before fitting data-dependent preprocessing or selecting features.
3. Within each permitted training partition, fit imputation, scaling and feature selection for each omics block; apply the fitted transforms to validation/test/prediction data and record the methods.
4. Run baseline models first: ElasticNet, XGBoost, LightGBM, SVR, GBLUP/rrBLUP where genotype features are present.
5. Train the DNNGP-style neural network with batch normalization, ReLU activation, dropout, early stopping, and tuned batch size.
6. Compare SNP-only, single-omics and fused models on the same eligible evaluation members and outer folds; disclose coverage differences. For late fusion, fit the fusion model using training-only information/inner OOF predictions and evaluate it on the outer holdout. Matching fold labels alone neither prove leakage nor establish independence.
7. Export feature importance from tree baselines, permutation importance, or SHAP-style methods; label DNN attributions as exploratory unless validated.

## Formal Execution

Use the upstream DNNGP project for exact execution. Do not substitute BioMind-local scripts when the user expects the TensorFlow DNNGP workflow.

## Outputs

- `single_omics_performance.tsv`
- `multiomics_performance.tsv`
- `predicted_trait.tsv`
- `feature_importance.tsv`
- `candidate_gene_importance.tsv`
- `omics_block_contribution.tsv`
- `report.md`

## Rules

- Do not train a deep model alone. Include simpler baselines and report whether DNNGP improves enough to justify complexity.
- Avoid leakage from normalization, feature filtering, or missing-value imputation across train/test folds.
- Warn when sample count is too small for deep learning; prefer ElasticNet/GBLUP/LightGBM for small datasets.
- For multiple traits, train and evaluate trait-specific models unless the user explicitly asks for multi-task learning.

## Related Skills

- `crop-breeding/crop-genomic-prediction` - choose the GS route and validation design.
- `crop-breeding/crop-gs-rrblup-gblup` - professional baseline SNP genomic prediction.
- `crop-breeding/crop-rnaseq-pipeline` - produce expression matrices from FASTQ/BAM.
- `crop-breeding/crop-conserved-transcriptome-ml` - transcriptome prediction with conserved response gene selection.
- `crop-breeding/crop-smart-breeding-strategy` - choose the correct prediction route.
