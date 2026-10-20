---
name: crop-cnn-attention-genomic-prediction
description: 'Train, plan, or interpret Cropformer-style crop genomic prediction using genotype encodings, CNN layers,
  multi-head self-attention, attention weights, SHAP, and haplotype follow-up. Use as the unweighted attention genomic
  prediction route when genotype and phenotype are available but GWAS-derived SNP weights are absent; use the weighted
  Transformer skill when fold-safe GWAS weights are central.

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

# Crop CNN Attention Genomic Prediction

Use this skill for Cropformer-style genomic prediction:

```text
genotype matrix + phenotype
-> genotype encoding
-> CNN local feature extraction
-> multi-head self-attention
-> phenotype prediction
-> attention / SHAP / haplotype interpretation
```

## Source Alignment

Read `references/source-notes.md` before using Cropformer-specific defaults. The upstream repository is PyTorch-based and expects users to edit paths in scripts.

## Environment And Data SOP

Read `references/install-and-data.md` before installing Cropformer, preparing upstream CSV inputs, or discussing model downloads. For formal biological conclusions, run the upstream Cropformer implementation and record the exact software version.

## Inputs

- `genotype_matrix.tsv`: marker matrix or preprocessed genotype CSV.
- `phenotype.tsv`: `sample_id`, trait columns, and optional environment/family columns.
- `marker_annotation.tsv` or `gff3`: optional marker coordinates and gene mapping.
- `haplotype_region.tsv`: optional regions for haplotype analysis after SNP prioritization.

## Task Scope And Shared Checks

Read `../_shared/gp-audit-and-data-validation.md` sections A–C for task evidence, matrix identity and observation checks. Apply the checks relevant to the requested analysis; complete a requested readiness audit even when a later training stage lacks inputs or resources. Keep design declarations, actual execution and stage-specific blockers separate.

## Workflow

1. Apply the shared matrix/ID checks, then validate marker encoding, complete sample/marker alignment and the evaluation split.
2. Train the professional rrBLUP/GBLUP baseline with `crop-breeding/crop-gs-rrblup-gblup` first.
3. If using a Cropformer-like route, choose marker filtering or feature selection inside the training fold.
4. Encode genotypes consistently; preserve marker position whenever interpretation is needed.
5. Train CNN + multi-head self-attention with fixed outer folds; early stopping and tuning use only the permitted inner training/validation data, with preprocessing fitted within that boundary.
6. Extract attention weights and run SHAP-style or permutation attribution where feasible.
7. Map prioritized SNPs to genes and optionally summarize haplotypes in candidate regions.

## Formal Execution

Use the upstream Cropformer repository for exact execution. Do not substitute BioMind-local scripts when the user expects the original PyTorch CNN/self-attention model.

## Outputs

- `predicted_trait.tsv`
- `model_performance.tsv`
- `important_snp.tsv`
- `attention_weight.tsv`
- `candidate_gene.tsv`
- `haplotype_candidates.tsv`
- `report.md`

## Rules

- Do not call attention weights causal evidence.
- If feature selection uses phenotype information, run it inside each training fold.
- Do not replace `crop-gp-waiter-transformer` when validated GWAS weights are available and the user asks for GP-WAITER.
- Warn about deep-learning instability for small sample sizes.

## Related Skills

- `crop-breeding/crop-genomic-prediction` - choose the GS route and validation design.
- `crop-breeding/crop-gs-rrblup-gblup` - professional rrBLUP/GBLUP baseline.
- `crop-breeding/crop-gp-waiter-transformer` - GWAS-weighted Transformer model.
- `crop-breeding/crop-gwas-pipeline` - GWAS and SNP-to-gene mapping.
- `crop-breeding/crop-smart-breeding-strategy` - choose appropriate prediction skill.
