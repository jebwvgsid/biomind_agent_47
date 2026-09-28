---
name: crop-smart-breeding-strategy
description: 'Route crop breeding prediction projects to the right BioMind skill based on available data: SNP phenotype
  genomic prediction, GWAS-weighted Transformer, multi-omics DNNGP, GxE environmental-index prediction, pangenome/SV
  genomic selection, transcriptome XGBoost, phenomics, or candidate gene workflows. Use when the user asks what
  machine-learning phenotype prediction strategy to use or wants an integrated smart-breeding plan rather than a
  single model run.

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

# Crop Smart Breeding Strategy

Use this as the routing and design skill for crop phenotype prediction. It does not run a model directly; it selects the right downstream BioMind skills and defines the validation plan.

## Source Alignment

Read `references/source-notes.md` when explaining the literature basis for smart breeding, integrated genomic-enviromic prediction, omics-to-breeding ML, or expanded genomic prediction.

## Decision Rules

| Available data | Recommended route |
| --- | --- |
| SNP + phenotype, standard additive GS | `crop-breeding/crop-gs-rrblup-gblup` |
| GS task needs method selection or data-readiness checks | `crop-breeding/crop-genomic-prediction` |
| SNP + phenotype, Bayesian GS requested | `crop-breeding/crop-gs-bglr-bayesian` |
| Multi-kernel, multi-trait, or GxE mixed-model GS | `crop-breeding/crop-gs-sommer-multikernel-gxe` |
| SNP + phenotype + GWAS p-values | `crop-breeding/crop-gp-waiter-transformer` |
| SNP + phenotype, no GWAS weights, deep model requested | `crop-breeding/crop-cnn-attention-genomic-prediction` |
| SNP + transcriptome/metabolome/other omics + phenotype | `crop-breeding/crop-dnngp-multiomics-prediction` |
| Multi-environment phenotype + envirotype/weather + SNP | `crop-breeding/crop-gxe-environmental-index-prediction` |
| SNP + SV/PAV/pangenome features + phenotype | `crop-breeding/crop-pangenome-sv-genomic-selection` |
| Expression + phenotype + ortholog/condition metadata | `crop-breeding/crop-conserved-transcriptome-ml` |
| Known GWAS markers/effects + new VCF samples | `crop-breeding/crop-wgs-to-marker-score` |
| Need candidate SNP-to-gene mapping | `crop-breeding/crop-gwas-pipeline` |

## Strategy Workflow

1. Inventory data: samples, traits, environments, omics blocks, variant classes, annotation, and missingness.
2. Define breeding goal: prediction accuracy, stable genotype selection, candidate gene mining, multi-trait selection, or new-environment forecasting.
3. Choose validation split before choosing models: random CV, leave-family-out, leave-year-out, leave-location-out, leave-genotype-out, or held-out target population.
4. Select a baseline-first plan. Deep learning must be compared against statistical or tree-based baselines unless the user asks for reproduction only.
5. Select interpretation output: marker effects, SHAP, attention, SNP-to-gene, haplotype, TF network, or environment sensitivity.
6. Produce a concise execution plan with selected BioMind skills and required inputs.

## Outputs

- `strategy_report.md`
- `selected_skills.tsv`
- `data_requirements.tsv`
- `validation_plan.tsv`
- `expected_outputs.tsv`

## Rules

- Do not recommend a high-impact deep model just because it is newer; match it to data type, sample size, and validation goal.
- Flag leakage risks explicitly: full-data GWAS weights, all-data normalization, phenotype-informed feature selection, and random CV across dependent environments.
- Prefer interpretable baseline models for small sample sizes and reserve deep models for sufficient sample size or explicit reproduction tasks.
- Separate prediction-for-selection from causal gene discovery.
