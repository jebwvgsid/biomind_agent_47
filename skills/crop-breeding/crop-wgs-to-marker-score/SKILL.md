---
name: crop-wgs-to-marker-score
description: Calculate crop breeding marker scores, favorable allele counts, or GEBV-like scores from WGS/GBS VCF genotypes and marker effect tables. Use for new crop material ranking from known GWAS or genomic prediction marker effects; avoid human clinical risk terminology and databases.
---

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
   `bash scripts/normalize_filter_vcf.sh --vcf samples.vcf.gz --reference genome.fa --out samples.norm.vcf.gz`
2. Score samples:
   `python scripts/apply_marker_effects.py --vcf samples.norm.vcf.gz --marker-effects marker_effects.tsv --trait brix --outdir results/marker_score`
3. Report:
   `python scripts/report_marker_score.py --scores results/marker_score/sample_marker_scores.tsv --coverage results/marker_score/marker_coverage.tsv --outdir results/marker_score`

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
