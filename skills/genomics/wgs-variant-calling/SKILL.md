---
name: wgs-variant-calling
description: Generate and validate WGS resequencing variant calling workflows from FASTQ/BAM through GATK gVCF, GenomicsDBImport, joint genotyping, VCF merge/filtering, PLINK LD pruning/PCA, SNP counting, and optional ANNOVAR-style SNP annotation. Use for large or many-sample genomes where intervals must be planned from FASTA .fai; keep chromosomes/contigs at or below 500 Mb unsplit and split only larger sequences unless the user overrides the threshold.
---

# WGS Variant Calling

Use this skill for WGS/resequencing pipelines that need robust GATK command generation, especially crop or non-model genomes with many chromosomes/contigs.

Aliases and trigger phrases: `gatk-wgs`, `wgs-gatk`, `resequencing-variant-calling`, WGS, gVCF, GenomicsDBImport, SNP filtering, PLINK, PCA, and ANNOVAR.

## Core Rules

- Do not hard-code chromosome names, chromosome counts, chromosome prefixes, or species-specific naming patterns. Derive intervals from `reference.fa.fai` and inspect the observed contig names before writing any `-L` values.
- Default interval threshold is `500000000` bp. Chromosomes/contigs at or below this length stay as one interval. Only split sequences larger than the threshold.
- Preserve FASTA `.fai` order through HaplotypeCaller, GenomicsDBImport, GenotypeGVCFs, and MergeVcfs.
- Keep `sample_bam.list` as two columns: `sample<TAB>bam_path`. If the input is one BAM path per line, normalize it before rendering scripts.
- Generate scripts first, inspect them, then submit to the scheduler or run them. Do not execute a full many-sample WGS pipeline in one opaque command.
- Keep comments off line-continuation lines in Bash commands; `\ # comment` breaks PLINK/GATK commands.
- Treat all paths, containers, species labels, annotation database names, ploidy, and filtering thresholds as project parameters unless the user explicitly provides them.

## Reusable Scripts

Generate interval files from a FASTA index:

```bash
python skills/genomics/wgs-variant-calling/scripts/make_gatk_interval_plan.py \
  --fai reference.fa.fai \
  --out-prefix intervals/wgs \
  --max-interval-bp 500000000
```

Render GATK/PLINK scripts from a normalized BAM list and interval plan:

```bash
python skills/genomics/wgs-variant-calling/scripts/render_gatk_wgs_scripts.py \
  --sample-bam-list sample_bam.list \
  --intervals-tsv intervals/wgs.intervals.tsv \
  --ref /abs/path/reference.fa \
  --container /abs/path/tools.sif \
  --outdir wgs_gatk_scripts
```

When rendering annotation commands, pass project-specific annotation settings instead of relying on defaults:

```bash
python skills/genomics/wgs-variant-calling/scripts/render_gatk_wgs_scripts.py \
  --sample-bam-list sample_bam.list \
  --intervals-tsv intervals/wgs.intervals.tsv \
  --ref /abs/path/reference.fa \
  --container /abs/path/tools.sif \
  --outdir wgs_gatk_scripts \
  --annotation-gff /abs/path/annotation.gff3 \
  --annotation-fasta /abs/path/reference.fa \
  --annovar-buildver project_reference_or_annotation_release
```

The renderer writes `00.preflight.sh`, `step1.HaplotypeCaller.sh`, `step2.GenomicsDBImport.sh`, `step3.GenotypeGVCFs.sh`, `step4.MergeVcfs.sh`, `step5.FilterSNPs.sh`, `step6.Plink_LD_PCA.sh`, and `step7.SNP_counts.sh`.

## Recommended Workflow

1. Prepare reference indexes:
   `samtools faidx reference.fa`; ensure BWA/GATK indexes are present before mapping or calling.
2. Prepare mapping commands if starting from FASTQ. Use `fastp -> bwa mem -> samtools sort -> sambamba markdup`, then index BAMs with `samtools index`.
3. Build `sample_bam.list` with two columns. Prefer:
   `sample<TAB>/abs/path/sample_mapping.sorted.markdup.bam`.
4. Run `make_gatk_interval_plan.py` on `reference.fa.fai`.
5. Run `render_gatk_wgs_scripts.py` and inspect the rendered commands.
6. Execute in order: HaplotypeCaller gVCF, GenomicsDBImport, GenotypeGVCFs, MergeVcfs, SNP filtering, PLINK LD/PCA, SNP counts, then optional annotation.

## FASTQ Mapping Template

Use this only when the user starts from paired FASTQ and has a valid `sample.list` with `sample fastq_1 fastq_2`.

```bash
while read -r sp fq1 fq2; do
  fastp --thread 16 -i "$fq1" -I "$fq2" \
    -o "${sp}_R1.filtered.fastq.gz" -O "${sp}_R2.filtered.fastq.gz" \
    -j "${sp}.fastp.json" -h "${sp}.fastp.html" 2>"${sp}.fastp.log"
  bwa mem -t 16 -R "@RG\tID:${sp}\tPL:illumina\tLB:lib1\tSM:${sp}" \
    reference.fa "${sp}_R1.filtered.fastq.gz" "${sp}_R2.filtered.fastq.gz" |
    samtools view -@ 4 -b -o "${sp}_mapping.bam"
  samtools flagstat -@ 16 "${sp}_mapping.bam" > "${sp}_flagstat.txt"
  samtools sort -@ 16 "${sp}_mapping.bam" -o "${sp}_mapping.sorted.bam"
  sambamba markdup -r -t 8 "${sp}_mapping.sorted.bam" "${sp}_mapping.sorted.markdup.bam"
  samtools index -@ 16 "${sp}_mapping.sorted.markdup.bam"
  samtools flagstat -@ 16 "${sp}_mapping.sorted.markdup.bam" > "${sp}_flagstat_markdup.txt"
done < sample.list
```

## Variant Filtering Defaults

The bundled renderer uses commonly used GATK hard filters for SNPs:

- `QD < 2.0`
- `QUAL < 30.0`
- `SOR > 3.0`
- `FS > 60.0`
- `MQ < 40.0`
- `MQRankSum < -12.5`
- `ReadPosRankSum < -8.0`

Treat these as starting points, not biological truth. Adjust or ask for confirmation when the organism, ploidy, sequencing depth, variant caller mode, or validation data differ from the assumptions behind diploid GATK hard filtering.

## Population Structure

Use PLINK after SNP filtering:

- missingness: `--geno 0.2`
- MAF: `--maf 0.05`
- biallelic: `--biallelic-only strict`
- LD pruning: `--indep-pairwise 50 10 0.5`
- PCA: `--pca 10`

These are defaults for a broad population-structure pass. Ask the user or infer from the study design before changing missingness, MAF, LD, or PCA component settings. For non-model references or non-human chromosome names, keep `--allow-extra-chr` and `--set-missing-var-ids @:#`.

## SNP Counts

Use `bcftools view -H -v snps input.vcf.gz | wc -l` for total SNP count.

For per-sample non-reference genotype counts, use the rendered `step7.SNP_counts.sh`; it preserves sample names with `bcftools query -l`.

## Annotation

Use ANNOVAR-style annotation only when the user provides reference FASTA, GFF3, and compatible ANNOVAR utilities in the container or PATH.

Annotation database selection is project-specific:

- Do not assume a database from a species label such as tobacco, crop, embryophyte, gymnosperm, animal, or microbe.
- If the user provides a project GFF3/FASTA, build a custom ANNOVAR-compatible database from those files and require a project-specific `ANNOVAR_BUILDVER` value.
- If the user provides an existing ANNOVAR database, use its documented `buildver`, `dbtype`, and database directory exactly.
- If taxonomy or annotation source is unclear, inspect filenames/metadata when available and ask for the annotation source instead of guessing.
- Keep `refGene` only when using the `gff3ToGenePred` route. Use another `dbtype` only when the provided database requires it.
- The renderer does not default `--annovar-buildver`. Provide `--annovar-buildver` during rendering, or set `ANNOVAR_BUILDVER` before running `step8.ANNOVAR_annotation.sh`. If neither is known, stop and ask the user.

Minimum command pattern for a custom GFF3/FASTA-derived database:

```bash
ANNOVAR_BUILDVER=project_specific_prefix
ANNOVAR_DBTYPE=refGene
ANNOVAR_DBDIR=annotation
gff3ToGenePred annotation.gff3 "$ANNOVAR_DBDIR/${ANNOVAR_BUILDVER}_${ANNOVAR_DBTYPE}.txt"
retrieve_seq_from_fasta.pl --format refGene --seqfile reference.fa \
  --out "$ANNOVAR_DBDIR/${ANNOVAR_BUILDVER}_${ANNOVAR_DBTYPE}Mrna.fa" \
  "$ANNOVAR_DBDIR/${ANNOVAR_BUILDVER}_${ANNOVAR_DBTYPE}.txt"
convert2annovar.pl -format vcf4 -allsample -withfreq gatk.filtered.snp.vcf.gz > all.snp.vcf.annovar.input
annotate_variation.pl -geneanno --neargene 2000 -buildver "$ANNOVAR_BUILDVER" -dbtype "$ANNOVAR_DBTYPE" \
  -outfile "$ANNOVAR_DBDIR/all.anno" -exonsort all.snp.vcf.annovar.input "$ANNOVAR_DBDIR"
```

## Gotchas

- `GenomicsDBImport` workspace paths must be unique per interval; remove or rename old workspaces before rerunning a failed interval.
- `g.vcf.gz` files must be indexed (`.tbi`) before GenomicsDBImport.
- Sample names in gVCF maps must be unique and match the BAM read group `SM` when possible.
- Use absolute reference/container paths in rendered scripts for cluster runs.
- For polyploid crops, confirm ploidy and downstream genotype assumptions before interpreting PCA or marker scores.

## Extended Reference

Read `references/wgs_gatk_notes.md` when adapting old shell snippets, deciding whether to skip interval splitting for small genomes, or explaining common command fixes.
