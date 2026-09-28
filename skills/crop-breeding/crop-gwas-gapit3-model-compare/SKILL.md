---
name: crop-gwas-gapit3-model-compare
description: 'Execute crop GWAS model comparison with GAPIT3, including MLM, MLMM, FarmCPU, and BLINK from user-provided
  phenotype, genotype, and optional covariate data. Use when the user wants multiple plant GWAS models and inflation/lead-SNP
  comparison; do not use for standalone GEMMA or rMVP runs.

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

# Crop GWAS GAPIT3 Model Compare

Use this operation skill when a crop GWAS should compare GAPIT3 models such as
MLM, MLMM, FarmCPU, and BLINK.

```bash
Rscript "${SKILL_DIR}/scripts/run_gapit3_models.R" \
  --genotype genotype_for_gapit.tsv \
  --phenotype phenotype.tsv \
  --trait flowering_time \
  --models MLM,MLMM,FarmCPU,BLINK \
  --covariates pcs_covariates.tsv \
  --outdir results/crop_gwas/gapit3_flowering_time
```

## Inputs

- `--genotype`: GAPIT-compatible genotype table.
- `--phenotype`: table with sample IDs and the target trait.
- `--trait`: phenotype column name.
- `--models`: comma-separated GAPIT3 models, usually `MLM,MLMM,FarmCPU,BLINK`.
- Optional `--covariates` with sample IDs and fixed covariates.

## Outputs

- GAPIT3 native outputs under `gapit_work/`
- standardized `summary_stats.tsv`, `lead_snps.tsv`, and `lambda_gc.tsv` when a GAPIT3 result table is produced
- `figures/`, `report.md`, `result.json`, `reproducibility/commands.sh`

## Rules

- Run only models explicitly listed in `--models`.
- Do not require a SNP to appear in multiple methods unless the user defines that consensus rule.
- Do not encode species, paper, or accession-specific thresholds inside the wrapper; pass thresholds and columns explicitly when needed.

## Related Skills

- `crop-breeding/crop-gwas-gemma-lmm` - GEMMA kinship LMM.
- `crop-breeding/crop-gwas-rmvp-large-scale` - rMVP route for large marker sets.
- `crop-breeding/crop-gwas-pav-sv-pangenome` - PAV/SV matrix GWAS.
