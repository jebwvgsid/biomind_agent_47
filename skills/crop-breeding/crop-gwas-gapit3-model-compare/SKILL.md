---
name: crop-gwas-gapit3-model-compare
description: >
  Execute crop GWAS model comparison with GAPIT3, including MLM, MLMM, FarmCPU,
  and BLINK from user-provided phenotype, genotype, and optional covariate data.
  Use when the user wants multiple plant GWAS models and inflation/lead-SNP
  comparison; do not use for standalone GEMMA or rMVP runs.
metadata:
  tags: [crop-breeding, GWAS, GAPIT3, MLM, MLMM, FarmCPU, BLINK]
  task_types: [crop_gwas]
  stage_tags: [gapit3_model_compare]
  inputs: [genotype_table, phenotype_table, trait, covariates, model_list]
  outputs: [summary_stats, lead_snps, lambda_gc, model_comparison, gwas_report]
  runtime:
    default_env: crop_gwas_r
    env_runner: conda run -n crop_gwas_r Rscript
    language: R
    packages: [GAPIT3, CMplot, data.table]
    capabilities: [crop-gwas-gapit3, farmcpu, blink, model-compare]
    healthcheck: conda run -n crop_gwas_r Rscript skills/crop-breeding/crop-gwas-gapit3-model-compare/scripts/run_gapit3_models.R --help
    install_policy: prefer-declared
    install_commands:
      - mamba create -n crop_gwas_r -c conda-forge r-base r-data.table r-remotes r-cmplot -y
      - conda run -n crop_gwas_r Rscript -e "remotes::install_github('jiabowang/GAPIT')"
---

# Crop GWAS GAPIT3 Model Compare

Use this operation skill when a crop GWAS should compare GAPIT3 models such as
MLM, MLMM, FarmCPU, and BLINK.

```bash
conda run -n crop_gwas_r Rscript skills/crop-breeding/crop-gwas-gapit3-model-compare/scripts/run_gapit3_models.R \
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
