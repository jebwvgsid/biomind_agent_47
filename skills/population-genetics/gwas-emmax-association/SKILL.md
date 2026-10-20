---
name: gwas-emmax-association
description: Run EMMAX mixed linear model GWAS from PLINK transposed genotype files, sorted phenotype, kinship matrix,
  and task-specified Q/PCA covariates. Use for fast SNP, InDel, or SV association testing when the target model is EMMAX
  and outputs must include p-values, logs, standardized Manhattan input, and validation summaries.
metadata:
  version: 1.0.1
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
| `COVARIATE` | EMMAX format `FID IID intercept covariates...`; mandatory when the task requires covariates |
| `PRECISION` | `10` for `-d` |

Use the executable/environment entry provided for this task. Set `EMMAX_BIN`
and `EMMAX_KIN_BIN` to those actual executables; the INTEL defaults below apply
when those are the declared tools. Do not add routine environment discovery or
version/help probes when the task already provides a verified entry. Resolve
an unknown option only when needed for the current command, within task scope.

Before association, verify the declared phenotype and covariate FID/IID order
against TFAM, finite covariates, and the fixed-effect design rank. Preserve the
original Q and use the task's declared columns. If required Q has no proven
full-row mapping, return `blocked_covariate_mapping` and stop dependent work.
Do not use intercept-only covariates, remove `-c`, substitute PCA, or rerun a
reduced model to complete this task. Resume after complete, version-matched
identity evidence is supplied; independent input QC follows the task's scope.

Compute kinship from the declared genotype prefix when an appropriate matrix
has not already been provided:

```bash
"${EMMAX_KIN_BIN:-emmax-kin-intel64}" \
  -v \
  -d "${PRECISION:-10}" \
  "${TPED_PREFIX}" \
  > "${OUTDIR}/logs/emmax.kinship.log" 2>&1
```

Set `KINSHIP` to the actual successful command's output. The official INTEL
example produces `<prefix>.aBN.kinf` without `-s` and `<prefix>.aIBS.kinf` with
`-s`; follow the task's estimator and the installed build's actual output.
Do not assume a universal kinship `-o` option or invent a filename. Validate K
dimensions, finite values, and sample-order provenance. For labeled K, any
sample reordering must be applied to both axes.

Run association with the validated covariates required by the task:

```bash
"${EMMAX_BIN:-emmax-intel64}" \
  -v \
  -d "${PRECISION:-10}" \
  -t "${TPED_PREFIX}" \
  -p "${PHENO_EMMAX}" \
  -k "${KINSHIP}" \
  -c "${COVARIATE}" \
  -o "${OUTDIR}/02.association/emmax" \
  > "${OUTDIR}/logs/emmax.log" 2> "${OUTDIR}/logs/emmax.err"
```

Omit `-c` only when the task's declared model has no such covariates. Missing
required covariate evidence is a blocked dependency, not permission to do so.

Standardize results by unique marker ID. The official INTEL `.ps` schema is
headerless `SNP_ID beta SE(beta) P`: one ID field and P in column 4. TPED supplies
CHR, marker ID, cM and BP in its first four fields; take coordinates from those
fields, not from the marker name. This converter supports the explicit
`emmax-intel4` schema and rejects other widths; a different version requires a
separately documented, named schema implementation rather than P-column guessing.

```bash
python "${SKILL_DIR}/scripts/emmax_to_manhattan.py" \
  --tped "${TPED_PREFIX}.tped" \
  --emmax "${OUTDIR}/02.association/emmax.ps" \
  --schema emmax-intel4 \
  --out "${OUTDIR}/02.association/emmax.manht_input.tsv" \
  > "${OUTDIR}/02.association/emmax_conversion_audit.json"
```

Check the exit status and audit `status=passed` before using the table. A
nonzero exit blocks result plotting and annotation even if an older output
still exists. The converter preserves the existing `SNP CHR BP P` columns,
reports counts and hashes, and publishes only after complete validation.

## 输出结果

Expected outputs:

- the actual kinship output recorded as `KINSHIP`
- `02.association/emmax.ps`
- `02.association/emmax.manht_input.tsv`
- `02.association/emmax_conversion_audit.json`
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

Require one unique result ID per TPED marker, exact ID-set equality, valid
positive coordinates, and finite P in `(0,1]`. Reordered results are joined by
ID and emitted in TPED order. Duplicate IDs, unexplained missing/extra IDs,
malformed rows and invalid P fail conversion; never truncate to the shorter
file, pair by row position, or silently filter. Reconcile TPED rows, result
rows, unique/matched IDs, valid P rows and output rows in the audit. Use the
successful `valid_tested_markers` count as M for a task-prescribed `0.05/M`
threshold; it validates result records, not the scientific model. A valid
analysis with no significant hits can produce empty hit/region tables. An
empty or failed result file is not that outcome.

Schema references: [official EMMAX INTEL guide](https://genome.sph.umich.edu/wiki/EMMAX)
and [PLINK TPED/MAP formats](https://www.cog-genomics.org/plink/1.9/formats#tped).

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| phenotype and TFAM sample counts differ | sample mismatch | rerun prep and inspect missing samples |
| kinship reused from another marker set | invalid model covariance | recompute kinship or document intentional reuse |
| Q/PCA file order wrong | covariates may be assigned incorrectly | establish full-row identity first; only then sort with `sort_phenotype.py` and verify TFAM order |
| `.ps` differs from the declared schema | output version or format differs | stop conversion and supply a documented named schema; never infer P from column position or numerical range |
| EMMAX binary missing | environment not ready | report blocked with exact command |

## 相似的 skills

- `population-genetics/gwas-vcf-prep-qc` - required preparation stage.
- `population-genetics/gwas-tassel-glm-mlm` - TASSEL GLM/MLM alternative.
- `population-genetics/gwas-manhattan-qq-plot` - plot `emmax.manht_input.tsv`.
- `crop-breeding/crop-gwas-gemma-lmm` - GEMMA LMM for crop-specific pipeline.
