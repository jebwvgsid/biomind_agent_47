---
name: gwas-emmax-association
description: Run EMMAX mixed linear model GWAS from PLINK transposed genotype files, sorted phenotype, kinship matrix, and optional Q/PCA covariates. Use for fast SNP, InDel, or SV association testing when the target model is EMMAX and outputs must include p-values, logs, standardized Manhattan input, and validation summaries.
version: "1.0.0"
tags: [GWAS, EMMAX, mixed-model, kinship, association, SNP, InDel, SV]
legacy_ids: [emmax-gwas, gwas-emmax, EMMAX关联分析]
task_types: [gwas_population_genetics]
stage_tags: [emmax_association, mixed_model_gwas]
inputs: [tped, tfam, emmax_phenotype, kinship, covariates]
outputs: [emmax_ps, kinship_matrix, manhattan_input, emmax_summary]
runtime:
  default_env: popgen_gwas
  env_runner: bash
  language: bash
  packages: [emmax, python]
  capabilities: [emmax-kinship, emmax-association, gwas-standardization]
  healthcheck: bash -lc "(emmax-intel64 2>&1 | head -1 >/dev/null || emmax 2>&1 | head -1 >/dev/null) && python skills/population-genetics/gwas-emmax-association/scripts/emmax_to_manhattan.py --help >/dev/null"
---

# GWAS EMMAX Association

Run EMMAX after `gwas-vcf-prep-qc`. EMMAX requires genotype, phenotype, kinship, and covariates to share the same sample order.

## 确认任务边界

Use this skill when:

- The user requests EMMAX, mixed linear model, kinship-corrected GWAS, or fast resequencing GWAS.
- Inputs are PLINK transposed files: `.tped` and `.tfam`.
- A sorted EMMAX phenotype file exists: `FID IID trait`.

Do not use this skill for TASSEL GLM/MLM, GEMMA, GAPIT3, or rMVP; route to their specific skills.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `TPED_PREFIX` | prefix without `.tped/.tfam` |
| `PHENO_EMMAX` | `FID IID trait` phenotype file |
| `OUTDIR` | output root |

Optional:

| Parameter | Default / rule |
|---|---|
| `KINSHIP` | compute from same marker prefix unless valid existing matrix is provided |
| `COVARIATE` | EMMAX format `FID IID intercept covariates...` |
| `PRECISION` | `10` for `-d` |

Kinship:

```bash
emmax-kin-intel64 \
  -v \
  -d "${PRECISION:-10}" \
  -o "${OUTDIR}/02.association/pop.kinship" \
  "${TPED_PREFIX}" \
  > "${OUTDIR}/logs/emmax.kinship.log" 2>&1
```

Association without covariates:

```bash
emmax-intel64 \
  -v \
  -d "${PRECISION:-10}" \
  -t "${TPED_PREFIX}" \
  -p "${PHENO_EMMAX}" \
  -k "${OUTDIR}/02.association/pop.kinship" \
  -o "${OUTDIR}/02.association/emmax" \
  > "${OUTDIR}/logs/emmax.log" 2> "${OUTDIR}/logs/emmax.err"
```

Add covariates only after order validation:

```bash
-c "${COVARIATE}"
```

Standardize result for plotting:

```bash
python skills/population-genetics/gwas-emmax-association/scripts/emmax_to_manhattan.py \
  --tped "${TPED_PREFIX}.tped" \
  --emmax "${OUTDIR}/02.association/emmax.ps" \
  --out "${OUTDIR}/02.association/emmax.manht_input.tsv"
```

## 输出结果

Expected outputs:

- `02.association/pop.kinship`
- `02.association/emmax.ps`
- `02.association/emmax.manht_input.tsv`
- `02.association/emmax_summary.tsv`
- `logs/emmax.log`
- `logs/emmax.err`

Validation:

```bash
test -s "${OUTDIR}/02.association/emmax.ps"
test -s "${OUTDIR}/02.association/emmax.manht_input.tsv"
awk 'NR>1 && ($4 <= 0 || $4 > 1){bad++} END{exit bad>0}' \
  "${OUTDIR}/02.association/emmax.manht_input.tsv"
```

Rows in `emmax.ps` should be close to markers in `.tped`; report mismatches before plotting.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| phenotype and TFAM sample counts differ | sample mismatch | rerun prep and inspect missing samples |
| kinship reused from another marker set | invalid model covariance | recompute kinship or document intentional reuse |
| Q/PCA file order wrong | covariates assigned incorrectly | sort with `sort_phenotype.py` |
| p-values not in final column | output version differs | inspect `emmax.ps` before conversion |
| EMMAX binary missing | environment not ready | report blocked with exact command |

## 相似的 skills

- `population-genetics/gwas-vcf-prep-qc` - required preparation stage.
- `population-genetics/gwas-tassel-glm-mlm` - TASSEL GLM/MLM alternative.
- `population-genetics/gwas-manhattan-qq-plot` - plot `emmax.manht_input.tsv`.
- `crop-breeding/crop-gwas-gemma-lmm` - GEMMA LMM for crop-specific pipeline.
