---
name: crop-gs-sommer-multikernel-gxe
description: 'Execute professional crop genomic prediction with CRAN sommer for multi-kernel, GxE, dominance, family/environment
  random effects, multi-trait, or complex mixed-model designs. Use only when a model configuration is explicitly
  defined.

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

# Crop GS sommer Multi-Kernel GxE

Use this operation skill for complex GS models that cannot be represented by a
single ordinary RR-BLUP/GBLUP run. The model must be defined in
`model_config.yaml` before execution.

## Inputs

- `model_config.yaml`: declares genotype, phenotype, trait, fixed effects,
  validation design, and sommer model route.
- Real genotype and phenotype files referenced by the config.
- Optional kernel matrices or feature blocks when the design is multi-kernel.

## Professional Command

```bash
Rscript "${SKILL_DIR}/scripts/run_sommer_multikernel_gxe.R" \
  --model-config model_config.yaml \
  --outdir results/gs_sommer
```

## Required Config Fields

- `genotype`
- `phenotype`
- `trait`
- `method`: currently `single_kernel_gblup` or a documented sommer formula route
- optional `fixed_effects`
- optional `group_column`

## Outputs

- `tables/prediction.tsv`
- `tables/ranked_breeding_materials.tsv`
- `tables/variance_components.tsv`
- `tables/model_performance.tsv`
- `model/final_model.rds`
- `reproducibility/sessionInfo.txt`
- `reproducibility/commands.sh`
- `report.md`

## Rules

- Do not guess random effects. If the config is incomplete, stop and ask for the
  biological model.
- Record the formula, kernels, fixed effects, random effects, and validation
  split.
- Use this skill for complex designs; use `crop-gs-rrblup-gblup` for ordinary
  single-kernel additive GS.
