---
name: crop-gwas-gemma-lmm
description: 'Execute crop GWAS with GEMMA kinship and linear mixed models from PLINK genotype data, phenotype vectors,
  and optional covariates or PCs. Use for rigorous single-population kinship-aware association testing; do not use
  for GAPIT3/rMVP model comparison or PAV/SV matrix GWAS.

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

# Crop GWAS GEMMA LMM

Use this operation skill after genotype QC when the analysis needs a classical
kinship-aware linear mixed model.

```bash
python "${SKILL_DIR}/scripts/run_gemma_lmm.py" \
  --bfile work/qc/genotypes_qc \
  --phenotype phenotype.txt \
  --trait flowering_time \
  --covariates pcs_covariates.txt \
  --outdir results/crop_gwas/gemma_flowering_time
```

## Inputs

- `--bfile` PLINK bed prefix, or `--pfile` to convert PLINK2 pfiles before GEMMA.
- `--phenotype`: GEMMA-compatible phenotype vector or table prepared by the user.
- `--trait`: trait label used in standardized outputs.
- Optional `--covariates` for PCs, population structure, trial block, year, or location.
- Optional `--gff` and `--window` for candidate gene mapping from lead SNPs.

## Outputs

- `summary_stats.tsv`
- `lead_snps.tsv`
- `lambda_gc.tsv`
- optional `candidate_genes.tsv`, `lead_snp_nearest_genes.tsv`, and `snp_window_genes.tsv`
- `figures/`, `report.md`, `result.json`, `reproducibility/commands.sh`

## Rules

- Use exact phenotype and covariate files supplied or prepared by the user; do not infer covariates from dataset names.
- Record whether the input was native PLINK bed or converted from PLINK2 pfiles.
- Keep GEMMA outputs separate from GAPIT3/rMVP outputs until final synthesis.

## Related Skills

- `crop-breeding/crop-gwas-qc-plink2-vcftools` - genotype QC and PCA.
- `crop-breeding/crop-gwas-gapit3-model-compare` - FarmCPU/BLINK and GAPIT3 model comparison.
- `crop-breeding/crop-gwas-rmvp-large-scale` - large-scale rMVP route.
