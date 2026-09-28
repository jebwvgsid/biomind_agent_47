---
name: crop-wgs-to-marker-score
description: Calculate crop breeding marker scores, favorable allele counts, or GEBV-like scores from WGS/GBS VCF
  genotypes and marker effect tables. Use for new crop material ranking from known GWAS or genomic prediction marker
  effects; avoid human clinical risk terminology and databases.
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

# Crop WGS to Marker Score

Use this skill when a crop VCF should be scored with known marker effects:

```text
WGS/GBS VCF + marker_effects.tsv -> dosage -> marker score/favorable allele count -> ranked samples
```

## Inputs

- `--input-vcf samples.vcf.gz`
- `--marker-effects marker_effects.tsv`
- `--trait brix`
- Optional `--sample-metadata sample_metadata.tsv`

`marker_effects.tsv` must contain `chr,pos,marker_id,effect_allele,effect_weight,trait`.
The marker-effect source must be traceable to a trained genomic prediction model, GWAS result, validated marker panel, or user-supplied breeding program table. Record the source, genome build, marker encoding, trait definition, and whether weights are additive effects, favorable-allele scores, or another scoring convention.

## Workflow

1. Normalize/filter/index VCF:
   `bash "${SKILL_DIR}/scripts/normalize_filter_vcf.sh" --vcf samples.vcf.gz --reference genome.fa --out samples.norm.vcf.gz`
2. Score samples:
   `python "${SKILL_DIR}/scripts/apply_marker_effects.py" --vcf samples.norm.vcf.gz --marker-effects marker_effects.tsv --trait brix --outdir results/marker_score`
3. Report:
   `python "${SKILL_DIR}/scripts/report_marker_score.py" --scores results/marker_score/sample_marker_scores.tsv --coverage results/marker_score/marker_coverage.tsv --outdir results/marker_score`

## Outputs

- `tables/sample_marker_scores.tsv`
- `tables/favorable_allele_counts.tsv`
- `tables/marker_coverage.tsv`
- `tables/ranked_samples.tsv`
- `figures/marker_score_distribution.png`
- `figures/top_samples_barplot.png`
- `report.md`, `result.json`, `reproducibility/commands.sh`

## Gotchas

- If marker coverage is below 50%, report a warning.
- Detect GT ploidy from `GT`; do not assume only `0/0`, `0/1`, `1/1`.
- Scores are marker-assisted or GEBV-like breeding scores, not clinical risk.
- Effect allele matching needs REF/ALT orientation; mark unresolved markers.
