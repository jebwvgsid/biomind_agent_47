---
name: gwas-vcf-prep-qc
description: Prepare resequencing VCF, phenotype, Q matrix, PCA covariates, and PLINK/EMMAX/TASSEL input files for
  GWAS. Use before SNP, InDel, or SV association testing when BioMind must filter markers, convert VCF to PLINK
  transposed files, sort phenotypes to genotype sample order, encode qualitative traits, prepare TASSEL files, or
  validate sample and chromosome compatibility.
metadata:
  version: 1.1.1
compatibility: 需要本技能 references/dependencies.md 列明的现有软件；不自动安装。输入输出位于分析项目。
---


## 资源定位与依赖

使用当前 Agent 的标准技能发现与文件读取能力加载本文件。`scripts/`、`references/`、
`assets/` 相对本 SKILL.md 的实际目录；输入、输出和样本清单相对本次分析项目。
命令中的 `${SKILL_DIR}` 必须先设为实际技能目录的绝对路径；该变量不由宿主自动提供。
例如，从已加载位置解析目录后，以带引号的脚本绝对路径执行，保持工作目录为分析项目，
不要在技能安装目录写结果。跨技能资源按其实际目录定位；分享完整技能包时保留目录结构。
软件和参考数据库来自已有环境或用户显式提供的位置。依赖见
[dependencies.md](references/dependencies.md)；安装、升级或修改环境需要事先明确授权。
本文命令可由任意提供命令执行能力的 Agent 或终端使用；不会在读取技能时安装或体检环境。

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
| `Q_MATRIX` | ADMIXTURE or structure covariates; required when the task specifies Q |
| `Q_COLUMNS` | exact covariate columns specified by the model; set before sorting |
| `N_PCS` | `3` when PCA is requested |
| `RECODE_MAP` | required for categorical traits |
| `IMPUTE` | off unless requested or justified by missingness |

Perform a bounded input-identity check for the current preparation step. Read
`references/input-identity.md` before choosing columns or interpreting Q order.
The standard-library helper can publish `gwas_input_structure` without a
scientific environment binding or durable report file:

```bash
python "${SKILL_DIR}/scripts/inspect_inputs.py" \
  --vcf "${VCF}" --phenotype "${PHENOTYPE}" --q-matrix "${Q_MATRIX}" \
  --observation-id gwas_input_structure
```

Omit `--q-matrix` when no Q was supplied; an empty path is not a valid Q input.
The helper uses standard-library Python and ordinary JSON. Its sibling
`input_contract.py` resolves from the script directory, independent of project
cwd. If a scoped check needs bcftools, use the actual available executable and
inspect its sample query results; a successful version probe is not sample
extraction. Unknown Q sample mapping must remain unknown.

Only a later approved preparation step creates its declared output directories.

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
python "${SKILL_DIR}/scripts/sort_phenotype.py" \
  --tfam "${OUTDIR}/01.prep/markers.tfam" \
  --phenotype "${PHENOTYPE}" \
  --trait "${TRAIT}" \
  --out-emmax "${OUTDIR}/01.prep/trait.emmax.txt" \
  --out-tassel "${OUTDIR}/01.prep/trait.tassel.txt"
```

For Q/PCA covariates with an explicit sample column, use the helper below. For
an unlabeled numeric matrix, obtain source-backed assignments for every data row
of the exact current Q file, then use `--q-row-map` and numeric column indexes
as documented in `references/input-identity.md`. Matching counts, an unrelated
sample list, or a few example rows do not establish full correspondence. Check
only task-provided inputs and their declared provenance; a supplement must be
shown to apply to this Q version before use. If unresolved, record
`blocked_covariate_mapping`, stop Q-dependent preparation and association, and
complete only task-authorized input QC that does not depend on Q. State the
full-row evidence needed to resume; do not add unrelated environment searches,
drop Q, or substitute new PCA/Q estimates.

The helper always adds an EMMAX intercept column. Preserve the original Q and
select the model's declared columns. If the task specifies intercept + Q1/Q2
for a three-component Q, set `Q_COLUMNS=Q1,Q2`; do not add Q3 or ask again.
Record the chosen columns and design rank. All three row-sum-one Q components
plus an intercept are linearly dependent. The helper does not check rank or
choose columns automatically; see the reference for this distinction.

```bash
python "${SKILL_DIR}/scripts/sort_phenotype.py" \
  --tfam "${OUTDIR}/01.prep/markers.tfam" \
  --phenotype "${Q_MATRIX}" \
  --input-kind q_covariates \
  --trait "${Q_COLUMNS}" \
  --out-tassel "${OUTDIR}/01.prep/qmatrix.tassel.txt" \
  --out-emmax-covar "${OUTDIR}/01.prep/qmatrix.emmax.txt" \
  --tassel-covariate true
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

Preserve the genotype's FID/IID pairs and exact ID strings. The bundled helper
matches by globally unique IID and carries FID into its output; it is not a
general composite-key join. If different families share an IID, stop this helper
and use an explicitly implemented, validated FID/IID adapter. Wrong order is
reported and reordered only after identities are established. Missing rows fail by default; only an
explicit `--allow-missing true` phenotype policy can emit missing values, and an
all-missing trait always fails. Extra/duplicate IDs, ambiguous columns and
numeric Q without identity are rejected before output creation. No-header input
requires numeric trait indexes (for example `--header absent --trait 2`).

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| sample IDs unmatched | genotype/phenotype not aligned | preserve original IDs; require a source-backed one-to-one map, and report unmatched IDs before reordering |
| categorical trait has text values | association tools need numeric phenotype | require explicit recoding map |
| PLINK duplicate marker IDs | marker names are not unique | audit duplicates and use a documented unique marker-ID mapping; `--set-missing-var-ids @:#` only fills missing IDs |
| Q matrix lacks sample column | row-to-sample mapping is unknown | obtain explicit row assignments or verifiable generating provenance, validate file versions and IDs; never paste a guessed order |
| imputation requested blindly | may alter marker distribution | run only when requested or justified |

Do not treat missing phenotype values as zero.

## 相似的 skills

- `population-genetics/gwas-emmax-association` - EMMAX after this prep.
- `population-genetics/gwas-tassel-glm-mlm` - TASSEL after this prep.
- `population-genetics/gwas-manhattan-qq-plot` - plot standardized association results.
- `crop-breeding/crop-gwas-qc-plink2-vcftools` - crop-specific PLINK2 QC route.
