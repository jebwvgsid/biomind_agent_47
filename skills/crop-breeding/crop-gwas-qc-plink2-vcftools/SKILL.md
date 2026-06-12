---
name: crop-gwas-qc-plink2-vcftools
description: >
  Execute crop GWAS genotype preprocessing with PLINK2 and standard VCF tooling:
  VCF/PLINK conversion, missingness/MAF/HWE filters, sample overlap checks,
  PCA, LD pruning, and optional Beagle imputation. Use before crop GWAS models;
  do not use for association testing or candidate gene interpretation.
metadata:
  tags: [crop-breeding, GWAS, QC, PLINK2, VCFtools, Beagle, PCA, LD]
  task_types: [crop_gwas]
  stage_tags: [genotype_qc]
  inputs: [vcf, pfile, bfile, phenotype_table, qc_parameters]
  outputs: [qc_genotype_prefix, sample_overlap, pca, ld_prune, qc_report]
  runtime:
    default_env: crop_gwas_cli
    env_runner: conda run -n crop_gwas_cli python
    language: python
    packages: [pandas, numpy, scipy, matplotlib, plink2, bcftools, vcftools, beagle, openjdk]
    capabilities: [crop-gwas-qc, plink2, vcf-qc, beagle-imputation]
    healthcheck: conda run -n crop_gwas_cli python skills/crop-breeding/crop-gwas-qc-plink2-vcftools/scripts/run_crop_gwas_qc.py --help
    install_policy: prefer-declared
    install_commands:
      - mamba create -n crop_gwas_cli -c conda-forge -c bioconda python=3.11 pandas numpy scipy matplotlib plink2 bcftools vcftools beagle openjdk -y
      - conda create -n crop_gwas_cli -c conda-forge -c bioconda python=3.11 pandas numpy scipy matplotlib plink2 bcftools vcftools beagle openjdk -y
---

# Crop GWAS QC with PLINK2/VCFtools

Use this operation skill for the genotype preprocessing stage of crop GWAS.

```bash
conda run -n crop_gwas_cli python skills/crop-breeding/crop-gwas-qc-plink2-vcftools/scripts/run_crop_gwas_qc.py \
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
