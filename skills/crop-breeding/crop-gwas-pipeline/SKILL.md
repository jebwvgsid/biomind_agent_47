---
name: crop-gwas-pipeline
description: >
  Route and plan crop or plant GWAS workflows across genotype QC, GEMMA LMM,
  GAPIT3 model comparison, rMVP large-scale GWAS, and PAV/SV pangenome GWAS.
  Use as the top-level entry skill for crop GWAS requests; delegate execution
  to the specific crop GWAS subskill instead of running analysis directly.
metadata:
  tags: [crop-breeding, GWAS, plant-GWAS, router, workflow]
  task_types: [crop_gwas]
  stage_tags: [gwas_triage]
  inputs: [data_inventory, genotype_data, phenotype_table, trait, covariates, marker_type]
  outputs: [selected_gwas_skill, data_requirements, method_plan, interpretation_boundaries]
  legacy_ids: [crop-gwas, plant-gwas, gwas-pipeline]
---

# Crop GWAS Pipeline Router

Use this skill as the top-level router for crop and plant GWAS. It chooses the
right operation skill and keeps the workflow staged. It is not the execution
skill for QC or association testing.

## Scope

- SNP/InDel GWAS from VCF, PLINK bed, or PLINK2 pfile data.
- Crop phenotype association with continuous or binary traits.
- PCA, kinship, covariates, LD pruning, missingness, and MAF decisions.
- GEMMA LMM, GAPIT3 MLM/MLMM/FarmCPU/BLINK, rMVP GLM/MLM/FarmCPU.
- PAV, SV, and pangenome marker-matrix association when non-SNP markers are central.

## Subskill Routing

| User need | Use |
|---|---|
| VCF/PLINK conversion, sample overlap, MAF/missingness/HWE filters, PCA, LD pruning, Beagle imputation | `crop-breeding/crop-gwas-qc-plink2-vcftools` |
| Classical kinship-aware LMM with GEMMA | `crop-breeding/crop-gwas-gemma-lmm` |
| Compare GAPIT3 MLM, MLMM, FarmCPU, and BLINK | `crop-breeding/crop-gwas-gapit3-model-compare` |
| Large marker set or explicit rMVP request | `crop-breeding/crop-gwas-rmvp-large-scale` |
| Gene presence-absence, structural variants, graph/pangenome marker matrix | `crop-breeding/crop-gwas-pav-sv-pangenome` |
| GWAS-derived weights for genomic prediction | run GWAS first, then route to `crop-breeding/crop-gp-waiter-transformer` only with fold-safe weights |
| Candidate locus population-genetic follow-up | `crop-breeding/crop-popgen-dnasp` after lead intervals are defined |

## Planning Workflow

1. Identify species, ploidy, genome assembly, genotype source, phenotype table, trait, sample count, marker count, and covariates.
2. Run or require `genotype_qc` before association unless the user provides a validated QC genotype prefix.
3. Choose association methods from explicit intent and data shape:
   - GEMMA for conservative kinship-aware LMM.
   - GAPIT3 for plant GWAS model comparison and FarmCPU/BLINK.
   - rMVP for larger marker sets or rMVP-specific requests.
   - PAV/SV skill when the marker matrix is non-SNP pangenome variation.
4. Keep each method output under a method-specific directory and synthesize only after standardized `summary_stats.tsv`, `lead_snps.tsv`, and `lambda_gc.tsv` exist.
5. Map lead SNPs to genes only with explicit marker coordinates and a matching GFF3 assembly.

## Output Organization

Prefer stable roots:

```text
results/crop_gwas/qc/
results/crop_gwas/gemma_<trait>/
results/crop_gwas/gapit3_<trait>/
results/crop_gwas/rmvp_<trait>/
results/crop_gwas/pav_sv_<trait>/
results/crop_gwas/synthesis/
```

## Hard Boundaries

- Do not run commands from this router; load the operation skill and use its documented script.
- Do not write species, paper, accession, or file-name special cases as fallback behavior.
- Do not compare polyploid dosage data with diploid defaults unless the encoding and model assumptions are explicit.
- Do not treat GWAS lead SNPs as causal proof without fine mapping, expression, haplotype, or validation evidence.

## Compatibility Scripts

The legacy scripts under this skill path are compatibility entrypoints. Prefer
the new canonical scripts in the routed subskills for new plans.
