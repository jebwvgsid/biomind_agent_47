---
name: crop-gp-waiter-transformer
description: 'Run or plan GP-WAITER-style crop phenotype prediction with GWAS-derived SNP weights, weighted embedding,
  CNN feature extraction, Transformer self-attention, SHAP/attention interpretation, and SNP-to-gene follow-up.
  Use when genotype markers, phenotype values, and GWAS p-values or summary statistics are available for crop genomic
  prediction; prefer crop-cnn-attention-genomic-prediction when no GWAS weights exist.

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

# Crop GP-WAITER Transformer

Use this skill for high-priority crop phenotype prediction when GWAS-derived SNP weights are available:

```text
genotype matrix + phenotype + GWAS p-values
-> -log10(p) SNP weights
-> weighted SNP embedding
-> CNN + Transformer prediction
-> SHAP / attention interpretation
-> candidate SNP and gene report
```

## Source Alignment

Read `references/source-notes.md` before claiming paper-specific settings or reproducing the original model. The upstream implementation is an Apache-2.0 GitHub release with a compiled GP-WAITER model component; do not copy or reimplement hidden internals unless the user explicitly asks for a faithful reproduction from upstream code.

## Environment And Data SOP

Read `references/install-and-data.md` before installing GP-WAITER, checking the compiled `GP_WAITER.so`, generating GWAS weights, or discussing model downloads. For formal biological conclusions, run the upstream GP-WAITER implementation and record the exact software version.

## Inputs

- `genotype_matrix.tsv`: rows are samples, columns are SNP markers, values are normalized genotype encodings.
- `phenotype.tsv`: contains `sample_id` and one or more trait columns.
- `gwas_summary.tsv`: contains marker ID and p-value columns aligned to genotype markers.
- `marker_annotation.gff3` or SNP coordinate table: optional, for SNP-to-gene mapping.
- `covariates.tsv`: optional population structure, batch, environment, family, or year covariates.

## Task Scope And Shared Checks

Read `../_shared/gp-audit-and-data-validation.md` sections A–C for task evidence, matrix identity and observation checks. Apply the checks relevant to the requested analysis; complete a requested readiness audit even when a later training stage lacks inputs or resources. Keep design declarations, actual execution and stage-specific blockers separate.

## Workflow

1. Apply the shared matrix/ID checks; verify sample/marker order, missingness, phenotype scale and the provenance of GWAS weights.
2. Fix the prediction target and outer evaluation split before generating weights. Use GWAS fitted only within the permitted training partition or documented independent external weights; then convert p-values with `-log10(p)`, cap infinite values and record the cap rule.
3. Train a professional baseline first with `crop-breeding/crop-gs-rrblup-gblup`; compare any weighted deep model against the same validation split.
4. Train GP-WAITER-style model only after the baseline split and metrics are fixed.
5. Match grouped or environment/year-held-out validation to the stated prediction target and dependency structure. For known genotypes in missing environments, state which historical observations are permitted.
6. Evaluate Pearson r, Spearman rho, MSE, RMSE, MAE, R2, training time, peak memory when available, and fold-level variance.
7. Interpret the model with attention weights and SHAP-style feature attribution; map high-priority SNPs to genes only after checking marker coordinates.

## Formal Execution

Use the upstream GP-WAITER repository for exact execution. Do not substitute BioMind-local scripts when the user expects the GP-WAITER CNN/Transformer model.

## Outputs

- `prediction.tsv`: `sample_id`, observed trait if available, predicted trait, fold, model, and split.
- `model_performance.tsv`: metrics for GP-WAITER and all baseline models.
- `important_snps.shap.tsv`: marker ID, trait, attribution score, rank, allele encoding, and direction if available.
- `attention_weight.tsv`: marker or token attention summaries with head/layer metadata when available.
- `snp_to_gene.tsv`: nearest or overlapping genes, distance, feature type, and annotation source.
- `report.md`: data QC, model comparison, interpretation, limitations, and reproducibility notes.

## Rules

- Do not report independent test performance using GWAS weights fitted with that test set’s phenotypes. Verify training membership and weight provenance, including marker/reference compatibility. A fold-design table or matching fold numbers alone does not prove which labels entered weight fitting; use the shared evidence states.
- Do not report only the deep learning result. Always include statistical and machine-learning baselines unless the user explicitly asks for a pure GP-WAITER reproduction.
- Treat SHAP and attention as prioritization evidence, not causal validation.
- Warn when sample size is small relative to marker count or when GPU memory is likely to be limiting.

## Related Skills

- `crop-breeding/crop-gwas-pipeline` - generate fold-aware GWAS summaries and SNP-to-gene maps.
- `crop-breeding/crop-genomic-prediction` - choose the GS route and validation design.
- `crop-breeding/crop-gs-rrblup-gblup` - run the professional rrBLUP/GBLUP baseline.
- `crop-breeding/crop-cnn-attention-genomic-prediction` - use CNN + attention without GWAS-derived SNP weights.
- `crop-breeding/crop-smart-breeding-strategy` - choose among SNP, omics, environment, and pangenome prediction workflows.
