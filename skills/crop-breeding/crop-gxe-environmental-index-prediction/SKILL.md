---
name: crop-gxe-environmental-index-prediction
description: 'Plan or run CERIS-JGRA-style genotype-by-environment crop prediction by selecting environmental indices
  from weather or envirotype time series, fitting reaction norms, deriving genotype intercept and slope traits,
  and applying GWAS or genomic prediction to predict performance in new environments. Use for multi-location, multi-year,
  multi-environment breeding data; do not use for single-environment SNP-only prediction.

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

# Crop GxE Environmental Index Prediction

Use this skill to reinstate the environmental dimension in crop GWAS and genomic selection:

```text
multi-environment phenotype + environment time series + genotype
-> search environmental index
-> fit reaction norm per genotype
-> derive intercept and slope traits
-> GWAS / genomic prediction
-> predict target-environment performance
```

## Source Alignment

Read `references/source-notes.md` before using CERIS-JGRA-specific terminology or exact package behavior. The upstream CERIS-JGRA code is an R repository organized by crop examples and helper functions.

## Environment And Data SOP

Read `references/install-and-data.md` before installing CERIS-JGRA, preparing environmental time series, or claiming exact R-repository reproduction. Formal analysis must use the upstream CERIS-JGRA R workflow or a recorded R implementation of the same model.

## Inputs

- `phenotype_multi_environment.tsv`: `sample_id`, `environment_id`, trait, and optional replicate/block columns.
- `genotype_matrix.tsv`: markers for GWAS or genomic prediction of reaction-norm parameters.
- `environment_timeseries.tsv`: environment variables by environment, date, and parameter.
- `growth_stage_window.tsv`: crop stage, start/end date or thermal-time window, and candidate variables.
- `target_environment.tsv`: optional new-environment profile for prediction.

## Task Scope And Shared Checks

Read `../_shared/gp-audit-and-data-validation.md` sections A–C for task evidence, matrix identity and observation checks. Apply the checks relevant to the requested analysis; complete a requested readiness audit even when a later training stage lacks inputs or resources. Keep design declarations, actual execution and stage-specific blockers separate.

## Workflow

1. Apply the shared matrix/ID checks and distinguish genotype, environment, phenotype-observation and weather-record keys. Record raw observation counts separately from sample × environment cell counts. When replicate semantics are missing, retain original observations and report the uncertainty; do not default to deduplication or averaging.
2. Define candidate environmental indices as environment parameter plus crop growth window.
3. Fix the evaluation boundary before index/window selection. For each outer fold, use only its permitted training phenotypes for selection and any inner tuning; verify that the chosen index is computable in the target environments with compatible variables, units and windows.
4. Fit a reaction norm per genotype using only permitted training observations: phenotype as intercept plus slope times environmental index, with justified replicate/block or aggregation treatment. Record the observations and distinct environments actually used; check independent index values and parameter estimability.
5. Treat intercept and slope as derived traits for GWAS and genomic prediction; derive training targets within the evaluation boundary and exclude held-out phenotypes from their construction.
6. Predict intercept and slope for unobserved genotypes, then combine them with target environmental index values.
7. Rank stable genotypes by predicted mean, slope magnitude, and trait direction.

## Formal Execution

Use the upstream CERIS-JGRA repository for exact execution. Do not substitute BioMind-local Python scripts for environmental-index search, reaction-norm fitting, or GxE genomic prediction.

## Outputs

- `best_environmental_index.tsv`
- `reaction_norm_parameters.tsv`
- `intercept_gwas.tsv`
- `slope_gwas.tsv`
- `environment_specific_prediction.tsv`
- `stable_genotypes.tsv`
- `report.md`

## Rules

- Do not assume the same environmental index works for all traits; search and report per trait.
- For every outer fold, calculate the environmental-index range from that fold’s training environments and compare its held-out/target values with that range. Record outside-range values and extrapolation flags per fold; a full-data range cannot verify holdout interpolation.
- Keep intercept and slope interpretation separate: intercept is baseline performance, slope is environmental sensitivity.
- Use environment- or year-held-out validation for new-environment prediction, genotype holdout for new genotypes, and both when required. For known genotypes in missing environments, state which historical observations are allowed. A parsing/axis error is not evidence that the user omitted an ID mapping.

## Related Skills

- `crop-breeding/crop-gwas-pipeline` - GWAS for derived intercept and slope traits.
- `crop-breeding/crop-genomic-prediction` - choose the GS route and validation design.
- `crop-breeding/crop-gs-rrblup-gblup` - baseline genomic prediction for intercept/slope traits.
- `crop-breeding/crop-gp-waiter-transformer` - weighted Transformer prediction if fold-specific GWAS weights are available.
- `crop-breeding/crop-smart-breeding-strategy` - decide whether a GxE route is warranted.
