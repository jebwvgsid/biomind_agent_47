---
name: crop-gwas-rmvp-large-scale
description: >
  Execute large-scale crop GWAS with rMVP GLM, MLM, and FarmCPU from VCF or
  marker genotype inputs plus phenotype data. Use when marker count or memory
  pressure favors rMVP; do not use for GEMMA-only LMM or GAPIT3 BLINK workflows.
metadata:
  tags: [crop-breeding, GWAS, rMVP, GLM, MLM, FarmCPU, large-scale]
  task_types: [crop_gwas]
  stage_tags: [rmvp_large_scale_gwas]
  inputs: [vcf, genotype_table, phenotype_table, trait, threads]
  outputs: [summary_stats, lead_snps, lambda_gc, rmvp_outputs, gwas_report]
  runtime:
    default_env: crop_gwas_r
    env_runner: conda run -n crop_gwas_r Rscript
    language: R
    packages: [rMVP, CMplot, bigmemory, data.table]
    capabilities: [crop-gwas-rmvp, glm, mlm, farmcpu, large-scale-gwas]
    healthcheck: conda run -n crop_gwas_r Rscript skills/crop-breeding/crop-gwas-rmvp-large-scale/scripts/run_rmvp_gwas.R --help
    install_policy: prefer-declared
    install_commands:
      - mamba create -n crop_gwas_r -c conda-forge r-base r-rmvp r-cmplot r-bigmemory r-data.table -y
      - conda create -n crop_gwas_r -c conda-forge r-base r-rmvp r-cmplot r-bigmemory r-data.table -y
---

# Crop GWAS rMVP Large-Scale

Use this operation skill when the GWAS should run through rMVP for large SNP
sets or when the user requests rMVP GLM/MLM/FarmCPU.

```bash
conda run -n crop_gwas_r Rscript skills/crop-breeding/crop-gwas-rmvp-large-scale/scripts/run_rmvp_gwas.R \
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
