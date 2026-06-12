---
name: crop-gwas-pav-sv-pangenome
description: >
  Execute crop PAV/SV/pangenome matrix GWAS from gene presence-absence,
  structural-variant, or graph/pangenome marker matrices plus phenotype data.
  Use when non-SNP pangenome markers are central; do not use for ordinary
  SNP-only PLINK/GEMMA/rMVP analysis.
metadata:
  tags: [crop-breeding, GWAS, pangenome, PAV, SV, FarmCPU, MLM]
  task_types: [crop_gwas]
  stage_tags: [pav_sv_pangenome_gwas]
  inputs: [marker_matrix, phenotype_table, trait, covariates, model]
  outputs: [summary_stats, lead_snps, lambda_gc, candidate_genes, pangenome_gwas_report]
  runtime:
    default_env: crop_gwas_r
    env_runner: conda run -n crop_gwas_r Rscript
    language: R
    packages: [GAPIT3, CMplot, data.table]
    capabilities: [crop-gwas-pav-sv, pangenome-gwas, farmcpu, mlm]
    healthcheck: conda run -n crop_gwas_r Rscript skills/crop-breeding/crop-gwas-pav-sv-pangenome/scripts/run_pav_sv_gwas.R --help
    install_policy: prefer-declared
    install_commands:
      - mamba create -n crop_gwas_r -c conda-forge r-base r-data.table r-remotes r-cmplot -y
      - conda run -n crop_gwas_r Rscript -e "remotes::install_github('jiabowang/GAPIT')"
---

# Crop PAV/SV Pangenome GWAS

Use this operation skill for association testing with non-SNP pangenome markers:
gene presence-absence, structural variants, graph-genome variants, or binary
marker matrices.

```bash
conda run -n crop_gwas_r Rscript skills/crop-breeding/crop-gwas-pav-sv-pangenome/scripts/run_pav_sv_gwas.R \
  --marker-matrix pav_matrix.tsv \
  --phenotype phenotype.tsv \
  --trait flowering_time \
  --model FarmCPU \
  --covariates population_covariates.tsv \
  --outdir results/crop_gwas/pav_sv_flowering_time
```

## Inputs

- `--marker-matrix`: sample-by-marker matrix with sample IDs in the first column.
- `--phenotype`: phenotype table with sample IDs and the target trait.
- `--trait`: phenotype column.
- `--model`: `MLM` or `FarmCPU`.
- Optional `--covariates` for PCs, population groups, trial design, or batch.

## Outputs

- native package outputs under `pav_sv_work/`
- standardized `summary_stats.tsv`, `lead_snps.tsv`, and `lambda_gc.tsv` when an association table is produced
- optional candidate gene tables after user-supplied annotation is mapped outside or after marker coordinates are available
- `figures/`, `report.md`, `result.json`, `reproducibility/commands.sh`

## Rules

- Keep SNP, SV, and PAV feature classes distinct in reports unless the user requests a combined model.
- Do not assume coordinates for PAV/SV marker IDs. Require explicit marker annotation before gene mapping.
- Do not use species-specific marker naming rules as fallback logic.

## Related Skills

- `crop-breeding/crop-pangenome-sv-genomic-selection` - pangenome-aware genomic prediction and breeding value comparison.
- `crop-breeding/crop-gwas-gapit3-model-compare` - SNP-focused GAPIT3 model comparison.
- `crop-breeding/crop-gwas-pipeline` - top-level crop GWAS router.
