---
name: crop-genomic-prediction
description: 'Guide crop genomic selection tasks by checking data readiness, leakage risk, validation design, and
  method fit before routing to a concrete professional GS skill. Use for task triage and quality boundaries; do
  not use as an execution skill or as a substitute for rrBLUP, BGLR, sommer, GEMMA, or BLUPF90.

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

# Crop Genomic Prediction Guide

Use this skill only as the genomic selection gatekeeper. It decides whether a
real crop GS task is ready to execute and which professional downstream skill
should be loaded.

## Responsibilities

1. Identify the biological task: prediction for selection, marker-effect
   estimation, multi-environment prediction, Bayesian comparison, or causal
   candidate discovery.
2. Check required data: species/ploidy, sample IDs, phenotype definition,
   genotype format, marker coding, covariates, trial design, family/year/location
   structure, and target population.
3. Choose the validation design before any model fitting or feature selection:
   independent K-fold, held-out family, held-out location/year, held-out
   environment, or external validation population.
4. Route to one concrete operation skill:
   - `crop-breeding/crop-gs-rrblup-gblup` for standard RR-BLUP/GBLUP.
   - `crop-breeding/crop-gs-bglr-bayesian` for Bayesian whole-genome regression.
   - `crop-breeding/crop-gs-sommer-multikernel-gxe` for multi-kernel, GxE,
     dominance, multi-trait, or complex random-effect designs.
5. State interpretation boundaries: GEBV supports selection decisions only after
   validation; marker effects and GWAS associations are prioritization evidence,
   not causal proof.

## Routing Rules

| Situation | Route |
| --- | --- |
| SNP/marker matrix plus one trait, additive model, ordinary GS baseline | `crop-breeding/crop-gs-rrblup-gblup` |
| Need Bayesian shrinkage priors, posterior marker effects, or RKHS comparison | `crop-breeding/crop-gs-bglr-bayesian` |
| Need multi-kernel, GxE, dominance, family/environment random effects, or multi-trait structure | `crop-breeding/crop-gs-sommer-multikernel-gxe` |
| Need association testing or GWAS-derived weights | Run a dedicated GWAS skill first, then use fold-safe outputs in the selected prediction skill |
| Need breeding-program scenario analysis | Use a separate scenario-analysis workflow outside this GS execution skill set |

## Hard Boundaries

- Do not run analysis from this guide.
- Do not invent a model from natural-language reasoning.
- Do not use self-written sklearn/base-R substitutes for formal GS.
- Do not include local validation data in this skill.
- Do not let full-data GWAS, feature filtering, imputation, normalization, or
  scaling leak information into validation folds.
- Do not use random row-wise CV when family, environment, location, year, trial
  block, or breeding population creates dependence.

## Handoff

When routing, produce these fields for the downstream skill:

- selected skill ID
- trait column and trait direction
- genotype source and marker coding
- phenotype/covariate files
- validation split and grouping column
- required package route and citation expectation
- missing data or design blockers
