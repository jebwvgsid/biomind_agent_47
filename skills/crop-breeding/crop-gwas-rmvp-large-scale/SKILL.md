---
name: crop-gwas-rmvp-large-scale
description: 'Execute large-scale crop GWAS with rMVP GLM, MLM, and FarmCPU from VCF or marker genotype inputs plus
  phenotype data. Use when marker count or memory pressure favors rMVP; do not use for GEMMA-only LMM or GAPIT3
  BLINK workflows.

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

# Crop GWAS rMVP Large-Scale

Use this operation skill when the GWAS should run through rMVP for large SNP
sets or when the user requests rMVP GLM/MLM/FarmCPU.

```bash
Rscript "${SKILL_DIR}/scripts/run_rmvp_gwas.R" \
  --vcf variants.vcf.gz \
  --phenotype phenotype.tsv \
  --trait plant_height \
  --models GLM,MLM,FarmCPU \
  --threads 8 \
  --outdir results/crop_gwas/rmvp_plant_height
```

## Inputs

- Exactly one of `--vcf` or `--genotype`.
- `--phenotype` with a sample column and target trait.
- `--trait` and optional `--models`.
- `--threads` for rMVP execution.

## Outputs

- rMVP native work files under `rmvp_work/`
- standardized `summary_stats.tsv`, `lead_snps.tsv`, and `lambda_gc.tsv` when an association table is found
- `figures/`, `report.md`, `result.json`, `reproducibility/commands.sh`

## Rules

- Use explicit genotype format arguments. Do not infer rMVP input type from file names beyond the `--vcf` or `--genotype` flag.
- Keep rMVP outputs method-labeled before any cross-method synthesis.
- Do not use this skill for PAV/SV matrix GWAS unless the user supplies rMVP-compatible numeric genotype input and explicitly asks for rMVP.

## Related Skills

- `crop-breeding/crop-gwas-qc-plink2-vcftools` - genotype QC.
- `crop-breeding/crop-gwas-gapit3-model-compare` - GAPIT3 BLINK and FarmCPU comparison.
- `crop-breeding/crop-gwas-gemma-lmm` - GEMMA LMM.
