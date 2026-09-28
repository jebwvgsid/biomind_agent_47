---
name: popgen-gwas-workflow
description: Route and supervise population genetics GWAS workflows from resequencing VCFs, phenotype tables, Q/PCA
  covariates, kinship, and gene annotations. Use when the user asks for SNP/InDel/SV GWAS, EMMAX, TASSEL GLM/MLM,
  Manhattan/QQ plots, significant loci, candidate genes, LDblock visualization, or population-structure-aware association
  planning. Do not use as the execution skill for crop-specific GEMMA/GAPIT/rMVP wrappers.
metadata:
  version: 1.1.0
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

## 初检与科学前置

Use the input-prep Skill's phase-local `inspect_inputs` (standard-library,
observation-only) or `inspect_vcf_samples` (verified bcftools) capability for the
actual initial operation. Do not require all later association/plotting packages
before that phase. A local successful probe is not whole-workflow readiness.
Consume the accepted `gwas_input_structure` observation from the preceding input inspection, retaining source hashes/read scope, phenotype header and columns,
ID set/order comparison, and Q mapping status.

No-header phenotype data are data from row one; use explicit column indexes.
A numeric 215-by-3 Q and 215 VCF samples establish count agreement only.
Without recorded row-to-sample correspondence, initial inspection/status may
complete, but Q covariate preparation and association_using_q must stop. Do not
invent a mapping, silently drop Q, or claim the requested MLM has completed.
A later change to a scientifically justified model belongs to the existing
explicit scientific decision and approval process. Read the input-prep Skill's
`references/input-identity.md` before resolving identity or selecting helper flags.

## 输出结果

The router should produce:

- selected canonical skill IDs.
- stage order and model decision.
- expected file contracts between stages.
- threshold and covariate policy.
- blocked status if sample IDs, phenotype, or dependencies are not ready.

Optional layout for stages that actually declare and produce files. This is not
a list of mandatory artifacts: initial `inspect_inputs` has `outputs=[]` and
publishes `gwas_input_structure`; it does not create `00.input_check/`. When a
sample-list file is needed, choose the file-producing capability and declare
that real file explicitly. Never create empty directories or sample files to
make an observation step pass. An already approved required output can change
only through the agreed scope change and approval process, not by dropping it
after execution. Keep the requested task-level reports as explicit requested results.

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
  stage_status.tsv
```

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| phenotype sample IDs do not match VCF | association invalid | stop and reconcile IDs |
| Q/PCA has known IDs in a different order | covariates need deterministic alignment | validate complete unique IDs and reorder to TFAM/VCF |
| numeric Q has no sample IDs | row correspondence remains unknown | obtain version-locked explicit row assignments or verifiable generating provenance; do not infer from counts |
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
