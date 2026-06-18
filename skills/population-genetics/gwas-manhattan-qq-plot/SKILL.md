---
name: gwas-manhattan-qq-plot
description: Normalize GWAS association results and generate Manhattan plots, QQ plots, significant-site tables, lambda GC summaries, and standardized SNP/CHR/BP/P tables from EMMAX, TASSEL, or other GWAS outputs. Use after association testing when the user needs publication-style GWAS plots or genome-wide thresholds. Do not use before p-values are validated.
version: "1.0.0"
tags: [GWAS, Manhattan, QQ, lambda-GC, CMplot, qqman, significant-loci]
legacy_ids: [gwas-plot, manhattan-qq, GWAS曼哈顿图, QQ图]
task_types: [gwas_population_genetics]
stage_tags: [gwas_plotting, significant_loci]
inputs: [gwas_table, pvalue_threshold]
outputs: [manhattan_png, manhattan_pdf, qq_png, qq_pdf, significant_sites, plot_summary]
runtime:
  default_env: popgen_gwas_r
  env_runner: Rscript
  language: R
  packages: [R, CMplot, qqman]
  capabilities: [manhattan-plot, qq-plot, lambda-gc]
  healthcheck: bash -lc "Rscript skills/population-genetics/gwas-manhattan-qq-plot/scripts/plot_gwas.R --help >/dev/null"
---

# GWAS Manhattan QQ Plot

Use this skill after EMMAX, TASSEL, GEMMA, GAPIT, rMVP, or another association stage has produced p-values.

## 确认任务边界

Use this skill when:

- A GWAS result table can be normalized to `SNP CHR BP P`.
- The user asks for Manhattan, QQ, significant loci, top hits, thresholds, or lambda GC.

Do not use this skill to run association models. Do not plot p-values outside `(0, 1]`.

## 调用的参数

Required input format:

```text
SNP	CHR	BP	P
Chr01:18180	Chr01	18180	0.583283761
```

Parameters:

| Parameter | Default / rule |
|---|---|
| `INPUT` | standardized GWAS table |
| `OUT_PREFIX` | e.g. `03.plot/trait` |
| `THRESHOLD` | user value, or Bonferroni `0.05 / marker_count`; tutorial-style default `1e-6` only when requested |
| `TITLE` | trait/model label |

Plot:

```bash
Rscript skills/population-genetics/gwas-manhattan-qq-plot/scripts/plot_gwas.R \
  --input "${INPUT}" \
  --out-prefix "${OUT_PREFIX}" \
  --threshold "${THRESHOLD}" \
  --title "${TITLE}"
```

Threshold policy:

- Record both p-value threshold and `-log10(P)`.
- Bonferroni threshold is `0.05 / marker_count`.
- For LDBlockShow cutline, use `-log10(threshold)`, for example `1e-6 -> 6`.

## 输出结果

Expected outputs:

- `<out_prefix>.manhattan.png`
- `<out_prefix>.manhattan.pdf`
- `<out_prefix>.qq.png`
- `<out_prefix>.qq.pdf`
- `<out_prefix>.significant_sites.tsv`
- `<out_prefix>.plot_summary.tsv`

Validation:

```bash
test -s "${OUT_PREFIX}.manhattan.png"
test -s "${OUT_PREFIX}.qq.png"
awk 'NR>1 && ($4 <= 0 || $4 > 1){bad++} END{exit bad>0}' "${INPUT}"
```

Report marker count, chromosome count, threshold, significant-site count, and lambda GC.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| p-values include 0 | `-log10(0)` infinite | replace only with justified minimum or remove and report |
| TASSEL column order used directly | wrong P column | convert with `tassel_to_manhattan.py` |
| EMMAX `.ps` lacks coordinates | marker metadata missing | merge with `.tped` using `emmax_to_manhattan.py` |
| threshold not documented | significance not reproducible | write numeric cutoff and rationale |
| chromosomes sort strangely | mixed labels | use stable chromosome order and report labels |

## 相似的 skills

- `population-genetics/gwas-emmax-association` - produces EMMAX plotting input.
- `population-genetics/gwas-tassel-glm-mlm` - produces TASSEL plotting input.
- `population-genetics/gwas-ldblock-candidate-region` - follow up significant loci.
- `crop-breeding/crop-gwas-gemma-lmm` - crop-specific GEMMA outputs.
- `crop-breeding/crop-diff-visualizer` - not for GWAS; expression plots only.
