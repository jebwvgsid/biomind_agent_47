---
name: gwas-vcf-prep-qc
description: Prepare resequencing VCF, phenotype, Q matrix, PCA covariates, and PLINK/EMMAX/TASSEL input files for GWAS. Use before SNP, InDel, or SV association testing when BioMind must filter markers, convert VCF to PLINK transposed files, sort phenotypes to genotype sample order, encode qualitative traits, prepare TASSEL files, or validate sample and chromosome compatibility.
version: "1.0.0"
tags: [GWAS, VCF, PLINK, EMMAX, TASSEL, phenotype, covariates, PCA, Q-matrix, QC]
legacy_ids: [gwas-prep-qc, gwas-vcf-qc, emmax-tassel-prep, GWAS输入准备]
task_types: [gwas_population_genetics]
stage_tags: [gwas_input_prep, genotype_qc, phenotype_alignment]
inputs: [vcf, phenotype_table, trait, q_matrix, pca, marker_type]
outputs: [tped, tfam, emmax_phenotype, tassel_trait, covariate_files, qc_summary]
runtime:
  default_env: popgen_gwas
  env_runner: bash
  language: bash
  packages: [bcftools, plink, plink2, beagle, openjdk, python]
  capabilities: [vcf-qc, plink-conversion, phenotype-sorting, covariate-prep]
  healthcheck: bash -lc "bcftools --version >/dev/null && (plink --version >/dev/null || plink2 --version >/dev/null) && python skills/population-genetics/gwas-vcf-prep-qc/scripts/sort_phenotype.py --help >/dev/null"
---

# GWAS VCF Prep QC

Prepare genotype, phenotype, and covariate files before EMMAX or TASSEL association testing.

## 确认任务边界

Use this skill when:

- Input is SNP/InDel/SV VCF and a phenotype table.
- The next stage is EMMAX, TASSEL GLM, TASSEL MLM, or Manhattan plotting.
- BioMind must align sample order across genotype, phenotype, and covariates.
- Qualitative phenotypes need explicit numeric recoding.

Do not use this skill for crop-specific PLINK2/VCFtools wrappers when the user requests that route; use `crop-breeding/crop-gwas-qc-plink2-vcftools`.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `VCF` | marker VCF, bgzipped and indexed when possible |
| `PHENOTYPE` | table with sample ID and trait |
| `TRAIT` | target trait column |
| `OUTDIR` | output root |

Optional:

| Parameter | Default / rule |
|---|---|
| `MARKER_TYPE` | `snp`; use `indel` or `sv` for relaxed defaults |
| `MAF` | SNP `0.05`, InDel/SV `0.01` |
| `GENO` | SNP `0.1`, InDel/SV `0.5` |
| `Q_MATRIX` | ADMIXTURE or structure covariates |
| `N_PCS` | `3` when PCA is requested |
| `RECODE_MAP` | required for categorical traits |
| `IMPUTE` | off unless requested or justified by missingness |

Preflight:

```bash
mkdir -p "${OUTDIR}/00.input_check" "${OUTDIR}/01.prep" "${OUTDIR}/logs"
bcftools view -h "${VCF}" | tail
bcftools query -l "${VCF}" > "${OUTDIR}/00.input_check/vcf.samples.txt"
plink --version
```

Convert VCF to PLINK transposed format for EMMAX:

```bash
plink \
  --vcf "${VCF}" \
  --maf "${MAF:-0.05}" \
  --geno "${GENO:-0.1}" \
  --recode 12 transpose \
  --output-missing-genotype 0 \
  --out "${OUTDIR}/01.prep/markers" \
  --set-missing-var-ids @:# \
  --allow-extra-chr \
  --keep-allele-order \
  > "${OUTDIR}/logs/plink.recode.log" 2>&1
```

Sort phenotype and covariates:

```bash
python skills/population-genetics/gwas-vcf-prep-qc/scripts/sort_phenotype.py \
  --tfam "${OUTDIR}/01.prep/markers.tfam" \
  --phenotype "${PHENOTYPE}" \
  --trait "${TRAIT}" \
  --out-emmax "${OUTDIR}/01.prep/trait.emmax.txt" \
  --out-tassel "${OUTDIR}/01.prep/trait.tassel.txt"
```

For Q/PCA covariates:

```bash
python skills/population-genetics/gwas-vcf-prep-qc/scripts/sort_phenotype.py \
  --tfam "${OUTDIR}/01.prep/markers.tfam" \
  --phenotype qmatrix.tsv \
  --trait Q1,Q2,Q3 \
  --out-tassel "${OUTDIR}/01.prep/qmatrix.tassel.txt" \
  --out-emmax-covar "${OUTDIR}/01.prep/qmatrix.emmax.txt" \
  --tassel-covariate
```

## 输出结果

Expected outputs:

- `01.prep/markers.tped`
- `01.prep/markers.tfam`
- `01.prep/trait.emmax.txt`
- `01.prep/trait.tassel.txt`
- optional `01.prep/qmatrix.emmax.txt`
- optional `01.prep/qmatrix.tassel.txt`
- optional `01.prep/pca.eigenvec`
- `00.input_check/vcf.samples.txt`
- `logs/plink.recode.log`

Validation:

```bash
test -s "${OUTDIR}/01.prep/markers.tped"
test -s "${OUTDIR}/01.prep/markers.tfam"
awk 'END{print NR}' "${OUTDIR}/01.prep/markers.tfam"
awk 'END{print NR}' "${OUTDIR}/01.prep/trait.emmax.txt"
```

Phenotype row count must match genotype samples unless missing phenotypes are explicitly allowed and encoded as missing.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| sample IDs unmatched | genotype/phenotype not aligned | normalize IDs or create a sample map |
| categorical trait has text values | association tools need numeric phenotype | require explicit recoding map |
| PLINK duplicate marker IDs | marker names not stable | use `--set-missing-var-ids @:#` or documented marker IDs |
| Q matrix lacks sample column | covariates cannot be sorted safely | paste validated sample IDs before use |
| imputation requested blindly | may alter marker distribution | run only when requested or justified |

Do not treat missing phenotype values as zero.

## 相似的 skills

- `population-genetics/gwas-emmax-association` - EMMAX after this prep.
- `population-genetics/gwas-tassel-glm-mlm` - TASSEL after this prep.
- `population-genetics/gwas-manhattan-qq-plot` - plot standardized association results.
- `crop-breeding/crop-gwas-qc-plink2-vcftools` - crop-specific PLINK2 QC route.
