---
name: crop-gwas-qc-plink2-vcftools
description: 'Execute crop GWAS genotype preprocessing with PLINK2 and standard VCF tooling: VCF/PLINK conversion,
  missingness/MAF/HWE filters, sample overlap checks, PCA, LD pruning, and optional Beagle imputation. Use before
  crop GWAS models; do not use for association testing or candidate gene interpretation.

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

# Crop GWAS QC with PLINK2/VCFtools

Use this operation skill for the genotype preprocessing stage of crop GWAS.

```bash
python "${SKILL_DIR}/scripts/run_crop_gwas_qc.py" \
  --vcf variants.vcf.gz \
  --phenotype phenotype.tsv \
  --outdir results/crop_gwas/qc \
  --maf 0.05 \
  --geno 0.1 \
  --mind 0.1 \
  --hwe 1e-6 \
  --ld-prune "50 5 0.2"
```

## Inputs

- One genotype source: `--vcf`, `--pfile`, or `--bfile`.
- `--phenotype`: table with a sample column; override with `--sample-col` when needed.
- QC thresholds: `--maf`, `--geno`, `--mind`, `--hwe`, and `--max-alleles`.
- Optional `--impute-beagle` with `--beagle-jar` if the Beagle executable is not on PATH.

## Outputs

- `qc/sample_overlap.tsv`
- `qc/genotypes_qc.pgen/.pvar/.psam` when PLINK2 execution succeeds
- `qc/genotypes_qc.pca.eigenvec`
- optional `qc/genotypes_qc.ld_prune.*`
- `result.json`, `report.md`, `reproducibility/commands.sh`

## Rules

- Always check sample overlap between genotype and phenotype before association testing.
- Do not infer species-specific filters from paper names or dataset labels. Use explicit user parameters or documented defaults.
- Warn before using diploid biallelic filters for polyploid dosage data unless dosage encoding is already confirmed.

## Related Skills

- `crop-breeding/crop-gwas-gemma-lmm` - kinship-aware GEMMA LMM after QC.
- `crop-breeding/crop-gwas-gapit3-model-compare` - GAPIT3 MLM/MLMM/FarmCPU/BLINK after QC.
- `crop-breeding/crop-gwas-rmvp-large-scale` - rMVP large-scale GWAS after QC.
