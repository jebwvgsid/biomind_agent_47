# WGS GATK Notes

## Interval Policy

Use the FASTA index as the source of truth. The default threshold is 500 Mb:

- `length <= 500000000`: keep the full contig/chromosome as one interval.
- `length > 500000000`: split into windows no larger than 500 Mb.

This avoids unnecessary fragmentation for small chromosomes while still preventing very large chromosomes from becoming single long jobs.

For a small genome where total callable length is below the threshold and sample count is small, it is reasonable to run GATK without explicit `-L` and skip MergeVcfs. For a many-sample cohort, keep explicit interval planning for easier retry and resource control.

If there are many tiny scaffolds and scheduler overhead dominates, consider packing small contigs into interval groups only after confirming the local GATK version and GenomicsDB behavior with a pilot run. The bundled renderer intentionally keeps one GATK interval unit per row to preserve simple merge ordering.

Never infer interval names from a previous project, chromosome count, or naming convention. A tobacco project with 24 named chromosomes, a reference with `chr1`, a fragmented draft assembly with scaffold IDs, and a non-plant genome all use the same rule: inspect `.fai`, then decide intervals from the observed contigs and project resource limits.

## BAM List Normalization

Preferred `sample_bam.list`:

```text
sampleA  /abs/path/sampleA_mapping.sorted.markdup.bam
sampleB  /abs/path/sampleB_mapping.sorted.markdup.bam
```

If the list contains one BAM path per line, derive sample names carefully. Do not use only `cut -d '_' -f 1` when sample IDs may contain underscores.

## Common Fixes From Older Shell Snippets

- Add `samtools index` after markdup.
- Use two-column BAM lists for GATK script generation.
- Keep `retrieve_seq_from_fasta.pl --format` separated by a space.
- Use `--keep-allele-order`, not `--keep-allele-orde`.
- Do not place comments after a trailing `\` in PLINK commands.
- Prefer `.vcf.gz` outputs and indexes for large cohorts.
- Do not copy an ANNOVAR `buildver`, database directory, or taxonomy label from another project. Confirm the annotation source and use project-specific database names.

## Annotation Decision Rules

ANNOVAR gene annotation needs a database naming contract, not a biological guess:

1. If the user supplies GFF3 plus reference FASTA, build a custom database from those exact files.
2. Set `ANNOVAR_BUILDVER` to a short project-specific prefix chosen from the reference/annotation release or supplied by the user.
3. Use `refGene` when `gff3ToGenePred` generated the database. Use a different `dbtype` only when the provided database documents that type.
4. If the user mentions a taxonomic group but does not provide the actual annotation files or database release, ask for the annotation source. Do not map broad groups such as angiosperm, gymnosperm, crop, or tobacco to a database name automatically.
5. Before running annotation, verify that the database directory contains files matching `${ANNOVAR_BUILDVER}_${ANNOVAR_DBTYPE}.txt` and, for coding annotations, the matching transcript FASTA generated from the same reference.

## Minimal QC Checks

Before HaplotypeCaller:

```bash
test -s reference.fa
test -s reference.fa.fai
while read -r sample bam; do test -s "$bam"; test -s "${bam}.bai" -o -s "${bam%.bam}.bai"; done < sample_bam.list
```

After each stage:

- HaplotypeCaller: each expected `.g.vcf.gz` exists and has `.tbi`.
- GenomicsDBImport: each interval workspace directory exists.
- GenotypeGVCFs: each raw interval VCF exists and is non-empty.
- MergeVcfs: merged raw VCF order matches the interval TSV order.
- Filtering: count total SNPs and retained PASS SNPs.

## Old-to-New Command Mapping

Old project-specific `chr.list` generation such as `seq -w 1 24` should be replaced by:

```bash
samtools faidx reference.fa
python "${SKILL_DIR}/scripts/make_gatk_interval_plan.py" \
  --fai reference.fa.fai \
  --out-prefix intervals/wgs \
  --max-interval-bp 500000000
```

Old manual `gvcf.$chr.map` loops should be replaced by the renderer, which writes one map per interval and keeps paths consistent with HaplotypeCaller outputs.
