---
name: crop-gs-rrblup-gblup
description: 'Execute professional crop RR-BLUP or GBLUP genomic selection with CRAN rrBLUP. Use for standard additive
  marker models, GEBV estimation, marker effects, cross-validated prediction accuracy, and ranking breeding materials
  from real genotype and phenotype data.

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

# Crop GS rrBLUP/GBLUP

Use this operation skill for standard crop genomic selection with CRAN `rrBLUP`.
It is a concrete execution skill, not a guide.

## Inputs

- `genotype_matrix.tsv`: `sample_id` plus numeric marker dosage columns.
- `phenotype.tsv`: `sample_id` plus the target trait column.
- `--trait`: target trait column.
- `--method rrblup|gblup`.
- Optional `covariates.tsv` and `--covariate-cols` for fixed effects.
- Optional `--group-column` for family, year, location, or environment-held-out
  validation.

## Professional Command

```bash
Rscript "${SKILL_DIR}/scripts/run_rrblup_package.R" \
  --genotype genotype_matrix.tsv \
  --phenotype phenotype.tsv \
  --trait trait_name \
  --method gblup \
  --folds 5 \
  --outdir results/gs_rrblup
```

Add covariates or grouped validation when required:

```bash
Rscript "${SKILL_DIR}/scripts/run_rrblup_package.R" \
  --genotype genotype_matrix.tsv \
  --phenotype phenotype.tsv \
  --trait trait_name \
  --method rrblup \
  --covariates covariates.tsv \
  --covariate-cols PC1,PC2,year \
  --group-column family \
  --outdir results/gs_rrblup
```

## Outputs

- `tables/gebv_predictions.tsv`
- `tables/ranked_breeding_materials.tsv`
- `tables/model_performance.tsv`
- `tables/cross_validation_results.tsv`
- `tables/marker_effects.tsv` for RR-BLUP marker-effect models
- `tables/genomic_relationship_diag.tsv` for GBLUP models
- `model/final_model.rds`
- `reproducibility/sessionInfo.txt`
- `reproducibility/commands.sh`
- `report.md`

## Rules

- Use this only after the guide has fixed the validation design.
- Do not report formal results unless `rrBLUP` is installed and `sessionInfo()`
  is captured.
- Do not use random K-fold when grouped validation is biologically required.
- For polyploid crops, confirm dosage coding before running this diploid-style
  numeric marker matrix workflow.
