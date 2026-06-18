---
name: gwas-tassel-glm-mlm
description: Run TASSEL GWAS using GLM or MLM from resequencing VCF, TASSEL trait files, Q/PCA covariates, and optional kinship matrix. Use when the user asks for TASSEL, GLM, MLM, centered IBS kinship, fixed-effect covariates, or TASSEL-based SNP/InDel/SV association testing. Do not use for EMMAX, GEMMA, GAPIT3, or rMVP workflows.
version: "1.0.0"
tags: [GWAS, TASSEL, GLM, MLM, kinship, covariates, association]
legacy_ids: [tassel-gwas, gwas-tassel, TASSEL关联分析]
task_types: [gwas_population_genetics]
stage_tags: [tassel_glm, tassel_mlm]
inputs: [vcf, tassel_trait, tassel_covariate, tassel_kinship]
outputs: [tassel_association, kinship, manhattan_input, logs]
runtime:
  default_env: popgen_gwas
  env_runner: bash
  language: bash
  packages: [tassel, openjdk, python]
  capabilities: [tassel-glm, tassel-mlm, tassel-kinship, gwas-standardization]
  healthcheck: bash -lc "run_pipeline.pl -h >/dev/null 2>&1 && python skills/population-genetics/gwas-tassel-glm-mlm/scripts/tassel_to_manhattan.py --help >/dev/null"
---

# GWAS TASSEL GLM MLM

Run TASSEL association tests after `gwas-vcf-prep-qc`. Inspect TASSEL output headers before converting because result file names and columns vary by version.

## 确认任务边界

Use this skill when:

- The user explicitly requests TASSEL, GLM, MLM, centered IBS kinship, or TASSEL covariate workflow.
- Trait file begins with `<Trait>`.
- Covariate file begins with `<Covariate>` when used.

Do not use this skill for EMMAX, GEMMA, GAPIT3, rMVP, or expression GWAS.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `VCF` | TASSEL-readable VCF |
| `TRAIT_TASSEL` | trait file beginning with `<Trait>` |
| `MODEL` | `glm` or `mlm` |
| `OUTDIR` | output root |

Optional:

| Parameter | Rule |
|---|---|
| `COVARIATE_TASSEL` | Q/PCA covariates; usually omit last Q column with `-excludeLastTrait` |
| `KINSHIP` | required for MLM; compute if absent |
| `MEMORY` | `10g` for GLM, `20g` or more for MLM |

GLM with covariates:

```bash
run_pipeline.pl \
  -Xms512m -Xmx${MEMORY:-10g} \
  -fork1 -vcf "${VCF}" \
  -fork2 -t "${TRAIT_TASSEL}" \
  -fork3 -q "${COVARIATE_TASSEL}" -excludeLastTrait \
  -combine4 -input1 -input2 -input3 -intersect \
  -FixedEffectLMPlugin -endPlugin \
  -export "${OUTDIR}/02.association/glm" \
  > "${OUTDIR}/logs/tassel.glm.log" 2>&1
```

Kinship for MLM:

```bash
run_pipeline.pl \
  -Xms512m -Xmx${MEMORY:-10g} \
  -importGuess "${VCF}" \
  -KinshipPlugin -method Centered_IBS -endPlugin \
  -export "${OUTDIR}/02.association/kinship.txt" \
  -exportType SqrMatrix \
  > "${OUTDIR}/logs/tassel.kinship.log" 2>&1
```

MLM:

```bash
run_pipeline.pl \
  -Xms512m -Xmx${MEMORY:-20g} \
  -fork1 -vcf "${VCF}" \
  -fork2 -t "${TRAIT_TASSEL}" \
  -fork3 -q "${COVARIATE_TASSEL}" -excludeLastTrait \
  -fork4 -k "${KINSHIP:-${OUTDIR}/02.association/kinship.txt}" \
  -combine5 -input1 -input2 -input3 -intersect \
  -combine6 -input5 -input4 -mlm \
  -mlmVarCompEst P3D \
  -mlmCompressionLevel None \
  -export "${OUTDIR}/02.association/mlm" \
  > "${OUTDIR}/logs/tassel.mlm.log" 2>&1
```

Standardize output:

```bash
python skills/population-genetics/gwas-tassel-glm-mlm/scripts/tassel_to_manhattan.py \
  --input "${TASSEL_ASSOC_TABLE}" \
  --out "${OUTDIR}/02.association/tassel.${MODEL}.manht_input.tsv"
```

## 输出结果

Expected outputs:

- `02.association/glm*.txt` or `02.association/mlm*.txt`
- optional `02.association/kinship.txt`
- `02.association/tassel.<model>.manht_input.tsv`
- `logs/tassel.<model>.log`

Validation:

```bash
test -s "${OUTDIR}/02.association/tassel.${MODEL}.manht_input.tsv"
awk 'NR>1 && ($4 <= 0 || $4 > 1){bad++} END{exit bad>0}' \
  "${OUTDIR}/02.association/tassel.${MODEL}.manht_input.tsv"
```

Check sample intersection counts from logs and report unexpected sample loss.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| wrong association file chosen | TASSEL output names differ | inspect headers before conversion |
| MLM run without kinship | model incomplete | compute or provide kinship |
| VCF rejected | TASSEL parser/order issue | sort with TASSEL plugin or bcftools |
| covariate last column included incorrectly | collinearity in Q matrix | use `-excludeLastTrait` unless justified |
| p-value column not detected | header differs | pass explicit `--p-col`, `--snp-col`, `--chr-col`, `--bp-col` |

## 相似的 skills

- `population-genetics/gwas-vcf-prep-qc` - prepare TASSEL trait/covariate files.
- `population-genetics/gwas-emmax-association` - EMMAX mixed model.
- `population-genetics/gwas-manhattan-qq-plot` - plot standardized TASSEL result.
- `crop-breeding/crop-gwas-gapit3-model-compare` - GAPIT3 plant model comparison.
