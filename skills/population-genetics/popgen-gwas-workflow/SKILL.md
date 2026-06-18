---
name: popgen-gwas-workflow
description: Route and supervise population genetics GWAS workflows from resequencing VCFs, phenotype tables, Q/PCA covariates, kinship, and gene annotations. Use when the user asks for SNP/InDel/SV GWAS, EMMAX, TASSEL GLM/MLM, Manhattan/QQ plots, significant loci, candidate genes, LDblock visualization, or population-structure-aware association planning. Do not use as the execution skill for crop-specific GEMMA/GAPIT/rMVP wrappers.
version: "1.0.0"
tags: [GWAS, population-genetics, EMMAX, TASSEL, GLM, MLM, Manhattan, QQ, LDblock, candidate-genes]
legacy_ids: [gwas-popgen, population-gwas, popgen-gwas, GWAS群体遗传分析]
task_types: [gwas_population_genetics]
stage_tags: [gwas_triage, workflow_routing]
inputs: [vcf, phenotype_table, trait, q_matrix, pca_covariates, gff3, model_choice]
outputs: [selected_gwas_skill, stage_plan, output_contract, blocked_reason]
---

# Population Genetics GWAS Workflow

Use this top-level skill to choose the correct GWAS stage skills. It plans and supervises; it should not run every model by default.

## 确认任务边界

Classify the request by marker type, model, and requested deliverable:

| User goal | Required skills |
|---|---|
| Prepare VCF, phenotype, Q/PCA files | `population-genetics/gwas-vcf-prep-qc` |
| EMMAX mixed-model GWAS | `gwas-vcf-prep-qc` -> `gwas-emmax-association` -> `gwas-manhattan-qq-plot` |
| TASSEL GLM fixed-effect GWAS | `gwas-vcf-prep-qc` -> `gwas-tassel-glm-mlm` with `MODEL=glm` -> plot |
| TASSEL MLM kinship-corrected GWAS | `gwas-vcf-prep-qc` -> `gwas-tassel-glm-mlm` with `MODEL=mlm` -> plot |
| Significant loci and nearby genes | association output -> `gwas-ldblock-candidate-region` |
| Local LD heatmap plus gene track | association output -> `gwas-ldblock-candidate-region` |

Default to EMMAX for large resequencing cohorts when the user does not require TASSEL. Use TASSEL GLM only for fixed-effect covariate models; use TASSEL MLM when the user explicitly wants TASSEL kinship correction.

Do not use this generic workflow when the user asks for crop-specific GEMMA, GAPIT3, rMVP, or PAV/SV matrix wrappers; route to `crop-breeding/crop-gwas-pipeline`.

## 调用的参数

Collect these before execution:

| Parameter | Meaning |
|---|---|
| `VCF` | SNP, InDel, SV, or filtered marker VCF |
| `PHENOTYPE` | sample ID plus target trait |
| `TRAIT` | one phenotype column |
| `MODEL` | `emmax`, `tassel_glm`, `tassel_mlm` |
| `MARKER_TYPE` | `snp`, `indel`, `sv`, or mixed |
| `Q_MATRIX` / `PCA` | population structure covariates |
| `GFF3/GTF` | required for candidate genes |
| `OUTDIR` | stable output root |

Default marker filters are starting points only:

| Marker type | MAF | Missingness |
|---|---:|---:|
| SNP | `0.05` | `0.1` |
| InDel | `0.01` | `0.5` |
| SV | `0.01` | `0.5` |

## 输出结果

The router should produce:

- selected canonical skill IDs.
- stage order and model decision.
- expected file contracts between stages.
- threshold and covariate policy.
- blocked status if sample IDs, phenotype, or dependencies are not ready.

Recommended layout:

```text
outdir/
  00.input_check/
  01.prep/
  02.association/
  03.plot/
  04.significant_loci/
  05.ldblock/
  logs/
  report.md
  reproduce.sh
```

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| phenotype sample IDs do not match VCF | association invalid | stop and reconcile IDs |
| Q/PCA matrix order differs from genotype | covariates assigned to wrong samples | sort to TFAM/VCF sample order |
| missing phenotype encoded as `0` | false phenotype value | encode as missing and report excluded samples |
| user asks "all GWAS methods" | unclear compute/model target | run a planned subset or ask for model priority |
| crop-specific GEMMA/GAPIT/rMVP requested | better local wrapper exists | route to `crop-breeding/crop-gwas-pipeline` |

Never claim a GWAS is complete from logs alone; verify association table, p-value range, plots, and report.

## 相似的 skills

- `population-genetics/gwas-vcf-prep-qc` - VCF/phenotype/covariate preparation.
- `population-genetics/gwas-emmax-association` - EMMAX mixed-model GWAS.
- `population-genetics/gwas-tassel-glm-mlm` - TASSEL GLM/MLM.
- `population-genetics/gwas-manhattan-qq-plot` - plotting and significant-site extraction.
- `population-genetics/gwas-ldblock-candidate-region` - candidate genes and LDblock.
- `crop-breeding/crop-gwas-pipeline` - crop-specific GEMMA/GAPIT/rMVP/PAV/SV route.
- `crop-breeding/crop-popgen-dnasp` - population genetic statistics around candidate intervals.
