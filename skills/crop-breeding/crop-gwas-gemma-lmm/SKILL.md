---
name: crop-gwas-gemma-lmm
description: >
  Execute crop GWAS with GEMMA kinship and linear mixed models from PLINK
  genotype data, phenotype vectors, and optional covariates or PCs. Use for
  rigorous single-population kinship-aware association testing; do not use for
  GAPIT3/rMVP model comparison or PAV/SV matrix GWAS.
metadata:
  tags: [crop-breeding, GWAS, GEMMA, LMM, kinship]
  task_types: [crop_gwas]
  stage_tags: [gemma_lmm_gwas]
  inputs: [bfile, pfile, phenotype_table, trait, covariates]
  outputs: [summary_stats, lead_snps, lambda_gc, candidate_genes, gwas_report]
  runtime:
    default_env: crop_gwas_cli
    env_runner: conda run -n crop_gwas_cli python
    language: python
    packages: [pandas, numpy, scipy, matplotlib, plink2, gemma]
    capabilities: [crop-gwas-gemma, kinship-lmm]
    healthcheck: conda run -n crop_gwas_cli python skills/crop-breeding/crop-gwas-gemma-lmm/scripts/run_gemma_lmm.py --help
    install_policy: prefer-declared
    install_commands:
      - mamba create -n crop_gwas_cli -c conda-forge -c bioconda python=3.11 pandas numpy scipy matplotlib plink2 gemma -y
      - conda create -n crop_gwas_cli -c conda-forge -c bioconda python=3.11 pandas numpy scipy matplotlib plink2 gemma -y
---

# Crop GWAS GEMMA LMM

Use this operation skill after genotype QC when the analysis needs a classical
kinship-aware linear mixed model.

```bash
conda run -n crop_gwas_cli python skills/crop-breeding/crop-gwas-gemma-lmm/scripts/run_gemma_lmm.py \
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
