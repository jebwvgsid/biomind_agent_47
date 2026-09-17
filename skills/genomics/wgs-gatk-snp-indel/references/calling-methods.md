# Calling methods and examples
## 确认任务边界

For new FASTQ preflight candidates, inspect and reuse the parent wrapper in
`genomics/wgs-short-read-mapping/scripts/inspect_fastq_batch.py`. Put its exact
bound inputs, execution-log directory and adjustable short-check deadline in
the candidate before approval. Do not hide four captured child checks until
the batch ends or forward their nested effect markers. Its stderr item events
are display facts; preserve prefix-only coverage and UNKNOWN failures. Do not
apply its 60-second initial item limit to HaplotypeCaller or other computation,
or modify a previously approved script.

Use this skill when:

- Input is coordinate-sorted, duplicate-marked WGS BAMs aligned to the same reference.
- BAMs have read groups and indexes.
- The goal is SNP/InDel discovery, usually across a cohort.
- The user wants GATK HaplotypeCaller, GVCF, GenomicsDBImport, joint genotyping, or hard filtering.

Do not use this skill for:

- RNA-seq variants; use `genomics/rnaseq-variant-calling`.
- SV/CNV; use LUMPY/CNVnator/long-read skills.
- BAMs without read groups; go back to mapping/preparation.

Confirm sample ploidy before HaplotypeCaller when the user prohibits guessing
critical scientific parameters. Preserve an unknown value in Consultant's
existing unresolved handoff and the same Plan's ask_user dependency. Input
checks and mapping need not wait, but calling/filter decisions that depend on
this value must wait for the existing user-decision flow. General Plan approval
is not biological confirmation. Use only the answer interpreted through the
existing confirm_decision evidence; never infer from names or species labels.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `REFERENCE_FASTA` | FASTA with `.fai` and `.dict` |
| `BAM_LIST` | `sample<TAB>bam_path` preferred |
| `OUTDIR` | output root |
| `INTERVALS` | chromosomes or interval TSV for large genomes |

Recommended:

| Parameter | Default / rule |
|---|---|
| `THREADS` | 4-8 per job; GATK scaling is limited |
| `MEMORY` | choose per interval and sample count |
| `TMPDIR` | fast local scratch if available |
| `JOINT_MODE` | `GenomicsDBImport` for many samples, `CombineGVCFs` for small cohorts |
| `PLOIDY` | explicit confirmed sample ploidy; unresolved means ask before calling, no implicit 2 |

Preflight:

```bash
gatk --version
test -s "${REFERENCE_FASTA}.fai"
test -s "${REFERENCE_FASTA%.*}.dict" || test -s "${REFERENCE_FASTA}.dict"
samtools quickcheck -v $(cut -f2 "${BAM_LIST}")
samtools view -H "$(cut -f2 "${BAM_LIST}" | head -1)" | grep '^@RG'
```

Core per-sample GVCF pattern:

```bash
gatk --java-options "-Xmx${MEMORY} -Djava.io.tmpdir=${TMPDIR}" HaplotypeCaller \
  -R "${REFERENCE_FASTA}" \
  -I "${bam}" \
  -L "${interval}" \
  --sample-ploidy "${PLOIDY:?Confirmed sample ploidy is required}" \
  -ERC GVCF \
  -O "${OUTDIR}/03.variant_calling/gvcf/${sample}/${sample}.${interval}.g.vcf.gz"
```

Joint genotyping:

```bash
gatk GenomicsDBImport \
  --sample-name-map "gvcf.${interval}.map" \
  --genomicsdb-workspace-path "${OUTDIR}/03.variant_calling/genomicsdb.${interval}" \
  -L "${interval}" \
  --reader-threads "${THREADS:-4}" \
  --tmp-dir "${TMPDIR}"

gatk GenotypeGVCFs \
  -R "${REFERENCE_FASTA}" \
  -V "gendb://${OUTDIR}/03.variant_calling/genomicsdb.${interval}" \
  -O "${OUTDIR}/03.variant_calling/${interval}.raw.vcf.gz"
```

For small cohorts, `CombineGVCFs` is acceptable, but do not pass a
`sample<TAB>gvcf_path` sample map as `--variant`. `CombineGVCFs --variant`
expects each value to be an actual GVCF/VCF path. Use repeated `--variant`
arguments or a list containing only GVCF paths, then run `GenotypeGVCFs`:

```bash
gatk CombineGVCFs \
  -R "${REFERENCE_FASTA}" \
  --variant "${OUTDIR}/03.variant_calling/gvcf/S1/S1.g.vcf.gz" \
  --variant "${OUTDIR}/03.variant_calling/gvcf/S2/S2.g.vcf.gz" \
  -O "${OUTDIR}/03.variant_calling/cohort.combined.g.vcf.gz"

gatk GenotypeGVCFs \
  -R "${REFERENCE_FASTA}" \
  -V "${OUTDIR}/03.variant_calling/cohort.combined.g.vcf.gz" \
  -O "${OUTDIR}/03.variant_calling/cohort.raw.vcf.gz"
```

Rule of thumb:

- `sample<TAB>gvcf_path` map -> `GenomicsDBImport --sample-name-map`.
- individual GVCF paths -> `CombineGVCFs --variant ... --variant ...`.
- If GATK reports a path like `S1%09/path/to/S1.g.vcf.gz`, the command passed
  a sample map where a GVCF path was expected; switch to one of the two valid
  patterns above instead of retrying the same shape.

Illustrative hard filters for a confirmed diploid short-read WGS setting only; unresolved ploidy/filter applicability requires the existing user-decision flow before use:

```bash
gatk VariantFiltration -R "${REFERENCE_FASTA}" -V all.raw.snp.vcf.gz \
  --filter-expression "QD < 2.0 || MQ < 40.0 || FS > 60.0 || SOR > 3.0 || MQRankSum < -12.5 || ReadPosRankSum < -8.0" \
  --filter-name SNP_filter -O all.filter.snp.vcf.gz

gatk VariantFiltration -R "${REFERENCE_FASTA}" -V all.raw.indel.vcf.gz \
  --filter-expression "QD < 2.0 || FS > 200.0 || SOR > 10.0 || MQRankSum < -12.5 || ReadPosRankSum < -8.0" \
  --filter-name INDEL_filter -O all.filter.indel.vcf.gz
```

Use `SelectVariants --exclude-filtered` for final PASS-only files.

## 输出结果

Expected outputs:

- `03.variant_calling/gvcf/<sample>/*.g.vcf.gz` plus indexes.
- interval raw VCFs and merged `all.raw.vcf.gz`.
- `04.filter/all.filtered.snp.vcf.gz`.
- `04.filter/all.filtered.indel.vcf.gz`.
- `.tbi` or `.idx` indexes.
- `05.summary/*.bcftools.stats.txt`.
- `05.summary/variant_counts.tsv`.
- `logs/*.log`.

Validation:

```bash
gatk ValidateVariants -R "${REFERENCE_FASTA}" -V "${OUTDIR}/04.filter/all.filtered.snp.vcf.gz"
gatk ValidateVariants -R "${REFERENCE_FASTA}" -V "${OUTDIR}/04.filter/all.filtered.indel.vcf.gz"
bcftools stats "${OUTDIR}/04.filter/all.filtered.snp.vcf.gz" > "${OUTDIR}/05.summary/snp.stats.txt"
bcftools stats "${OUTDIR}/04.filter/all.filtered.indel.vcf.gz" > "${OUTDIR}/05.summary/indel.stats.txt"
```

Report raw and PASS counts, sample count, interval strategy, filter expressions, and ploidy.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| no read groups | GATK cannot track samples | fix BAM with read groups before calling |
| duplicate sample names in GVCFs | joint genotyping will fail or merge wrongly | normalize sample names and RG `SM` |
| GenomicsDB workspace already exists | rerun collision | use new workspace or remove only after user approval |
| contig mismatch | BAM/GVCF/reference differ | re-align or use matching reference |
| zero PASS variants | filters too strict or poor data | inspect raw counts and filter distribution before claiming failure |

Do not concatenate VCF text by hand. Use GATK `MergeVcfs`, Picard, or bcftools-aware tools.

## 相似的 skills

- `genomics/wgs-variant-calling` - related interfaces and interval planning; legacy renderer examples do not supply unresolved parameters.
- `genomics/wgs-short-read-mapping` - prepare BAMs from FASTQ.
- `genomics/wgs-bcftools-snp-indel` - faster lightweight SNP/InDel workflow.
- `genomics/rnaseq-variant-calling` - RNA-seq variant workflow.
- `genomics/snpeff-annotation` - annotate final VCF.
