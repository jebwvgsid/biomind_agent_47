---
name: crop-gs-bglr-bayesian
description: 'Execute Bayesian crop genomic selection with CRAN BGLR. Use for BRR, BayesA, BayesB, BayesC, Bayesian
  LASSO, and RKHS models when posterior shrinkage, marker-effect uncertainty, or Bayesian model comparison is required.

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

# Crop GS BGLR Bayesian

Use this operation skill when the selected GS method is Bayesian whole-genome
regression through CRAN `BGLR`.

## Inputs

- `genotype_matrix.tsv`: `sample_id` plus numeric marker dosage columns.
- `phenotype.tsv`: `sample_id` plus target trait.
- `--model BRR|BayesA|BayesB|BayesC|BL|RKHS`.
- `--niter`, `--burnin`, `--thin`, and `--seed`.
- Optional covariates and grouped validation.

## Professional Command

```bash
Rscript "${SKILL_DIR}/scripts/run_bglr_bayesian.R" \
  --genotype genotype_matrix.tsv \
  --phenotype phenotype.tsv \
  --trait trait_name \
  --model BRR \
  --niter 12000 \
  --burnin 2000 \
  --thin 5 \
  --outdir results/gs_bglr
```

## Outputs

- `tables/prediction.tsv`
- `tables/ranked_breeding_materials.tsv`
- `tables/model_performance.tsv`
- `tables/cross_validation_results.tsv`
- `tables/posterior_marker_effects.tsv` when the selected BGLR model exposes marker effects
- `model/final_model.rds`
- `reproducibility/sessionInfo.txt`
- `reproducibility/commands.sh`
- `report.md`

## Rules

- State the prior/model (`BRR`, `BayesA`, `BayesB`, `BayesC`, `BL`, or `RKHS`)
  in every report.
- Record MCMC settings and seed.
- Treat posterior marker effects as model-based prioritization evidence, not
  causal proof.
- Do not compare Bayesian and non-Bayesian models unless they share the same
  validation split.
