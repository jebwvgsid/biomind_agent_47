---
name: gwas-emmax-association
description: Run EMMAX mixed linear model GWAS from PLINK transposed genotype files, sorted phenotype, kinship matrix,
  and optional Q/PCA covariates. Use for fast SNP, InDel, or SV association testing when the target model is EMMAX
  and outputs must include p-values, logs, standardized Manhattan input, and validation summaries.
metadata:
  version: 1.0.0
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
python "${SKILL_DIR}/scripts/emmax_to_manhattan.py" \
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
