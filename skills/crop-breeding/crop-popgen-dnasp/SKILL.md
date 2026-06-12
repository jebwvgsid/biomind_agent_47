---
name: crop-popgen-dnasp
description: Analyze crop candidate genes or genomic intervals with DnaSP-like population genetics statistics from FASTA alignments or VCF plus regions and sample groups. Use for nucleotide diversity pi, Tajima's D, haplotype diversity, LD r2, Dxy/Da, SFS, Ka/Ks, and selection-signal summaries around GWAS or DEG candidate loci.
---

# Crop PopGen DnaSP

Use this skill for candidate gene/region population genetics in crop breeding:

```text
alignment or VCF + region + sample groups -> pi/TajimaD/LD/haplotypes/Dxy/KaKs -> breeding interpretation report
```

## Inputs

Mode A:

- `--alignment region.fasta`

Mode B:

- `--vcf population.vcf.gz`
- `--region-bed candidate_regions.bed`
- `--sample-group sample_group.tsv`
- `--reference-fasta genome.fa`
- Optional `--gff genome.gff3`

Analysis options: `polymorphism,ld,divergence,kaks,sfs`.

## Workflow

1. Extract candidate regions from VCF:
   `bash scripts/extract_region_from_vcf.sh --vcf population.vcf.gz --regions candidate_regions.bed --outdir work/regions`
2. Convert VCF to alignment when needed:
   `python scripts/vcf_to_fasta_alignment.py --vcf work/regions/region.vcf.gz --reference genome.fa --output region.fasta`
3. Compute statistics:
   `python scripts/run_popgen_stats.py --alignment region.fasta --sample-group sample_group.tsv --outdir results/popgen --window-size 10000 --step-size 5000`
4. Prepare haplotype network input:
   `python scripts/haplotype_network_input.py --alignment region.fasta --output haplotype_network_input.tsv`
5. Summarize candidate region:
   `python scripts/summarize_candidate_region.py --stats results/popgen/polymorphism_stats.tsv --outdir results/popgen`

## Outputs

- `tables/polymorphism_stats.tsv`
- `tables/tajimaD_by_region.tsv`
- `tables/pi_by_group.tsv`
- `tables/fst_or_dxy_by_group.tsv`
- `tables/haplotype_summary.tsv`
- `tables/ld_decay.tsv`
- `tables/kaks_results.tsv`
- `figures/pi_sliding_window.png`
- `figures/tajimaD_sliding_window.png`
- `figures/ld_decay.png`
- `figures/haplotype_network_input.tsv`
- `report.md`, `result.json`, `reproducibility/commands.sh`

## Breeding Interpretation

- Low pi may indicate a selection sweep or reduced diversity.
- Tajima's D < 0 may indicate recent positive selection or expansion.
- Tajima's D > 0 may indicate balancing selection or population structure.
- High Dxy/Da between high-trait and low-trait groups suggests candidate-region differentiation.
- Ka/Ks > 1 can suggest positive selection, but requires careful validation.

## Gotchas

- Do not assume human chromosome names; crop `Chr01`, `Chr1`, and scaffolds are valid.
- For CDS Ka/Ks, check length divisible by 3 and stop codons.
- Interpret population statistics together with sampling design and structure.

## Tests

Validate pi, Tajima's D, haplotypes, and LD outputs with independent validation data or user-provided alignments; do not keep local example population files inside this skill.
