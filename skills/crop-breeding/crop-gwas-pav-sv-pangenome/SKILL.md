---
name: crop-gwas-pav-sv-pangenome
description: 'Execute crop PAV/SV/pangenome matrix GWAS from gene presence-absence, structural-variant, or graph/pangenome
  marker matrices plus phenotype data. Use when non-SNP pangenome markers are central; do not use for ordinary SNP-only
  PLINK/GEMMA/rMVP analysis.

  '
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

# Crop PAV/SV Pangenome GWAS

Use this operation skill for association testing with non-SNP pangenome markers:
gene presence-absence, structural variants, graph-genome variants, or binary
marker matrices.

```bash
Rscript "${SKILL_DIR}/scripts/run_pav_sv_gwas.R" \
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
