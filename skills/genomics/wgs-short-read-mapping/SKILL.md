---
name: wgs-short-read-mapping
description: Map paired-end short-read WGS resequencing FASTQ to a reference genome with BWA-MEM or BWA-MEM2, samtools, and duplicate marking, producing coordinate-sorted indexed BAMs for downstream SNP/InDel, SV, and CNV calling. Use before WGS variant calling when users provide FASTQ rather than ready BAM files. Do not use for RNA-seq, long-read, or assembly comparison data.
version: "1.0.0"
tags: [WGS, resequencing, mapping, BWA, BWA-MEM2, samtools, Picard, sambamba, mark-duplicates, BAM]
legacy_ids: [short-read-mapping, bwa-mapping, wgs-bwa-mem, resequencing-mapping, 双端WGS fastq, WGS fastq比对, 先比对再做GATK]
task_types: [wgs_mapping]
stage_tags: [read_mapping, bam_preparation]
inputs: [reference_fasta, paired_fastq_sample_table, output_directory]
outputs: [sorted_markdup_bam, bam_index, flagstat, coverage, duplicate_metrics, bam_list]
runtime:
  default_env: bio_wgs
  env_runner: bash
  language: bash
  packages: [bwa, bwa-mem2, samtools, picard, sambamba]
  capabilities: [short-read-alignment, duplicate-marking, bam-qc]
  healthcheck: bash -lc "samtools --version >/dev/null && (bwa 2>&1 | head -1 >/dev/null || bwa-mem2 version >/dev/null) && (command -v picard >/dev/null || command -v sambamba >/dev/null)"
  install_policy: prefer-declared
  install_command: mamba create -n bio_wgs -c conda-forge -c bioconda -y bwa bwa-mem2 samtools picard sambamba
---

# WGS Short-Read Mapping

Use this skill to convert paired-end WGS FASTQ into duplicate-marked coordinate-sorted BAMs. The downstream caller decides whether duplicates should be marked or removed; for variant calling, default to **mark and keep**.

## 确认任务边界

Use this skill when:

- Input is Illumina/MGI/DNBSEQ-style paired-end WGS or resequencing FASTQ.
- The next stage is WGS SNP/InDel, Manta short-read SV, or CNVnator CNV.
- The user needs reference indexes and BAM QC.

Do not use this skill when:

- Reads are RNA-seq; use `genomics/rnaseq-variant-calling`.
- Reads are ONT/PacBio long reads; use `genomics/long-read-sv-sniffles2`.
- BAMs are already aligned, coordinate-sorted, indexed, and have read groups; go directly to the caller.

Before running, verify that FASTQ files are real project inputs, not tutorial/demo files.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `REFERENCE_FASTA` | reference genome FASTA |
| `SAMPLE_TABLE` | TSV with `sample fq1 fq2` |
| `OUTDIR` | output directory |

Recommended:

| Parameter | Default | Notes |
|---|---|---|
| `ALIGNER` | `bwa-mem2` if available, else `bwa mem` | keep command in report |
| `THREADS` | 8 or cluster-provided value | mapping and sorting |
| `SORT_MEM` | `1G` per thread | reduce on memory-limited nodes |
| `PLATFORM` | `illumina` | read group `PL` |
| `DUPLICATE_TOOL` | `picard` or `sambamba` | keep duplicates unless user asks removal |

Sample table:

```tsv
sample	fq1	fq2
S1	/path/S1_R1.fq.gz	/path/S1_R2.fq.gz
S2	/path/S2_R1.fq.gz	/path/S2_R2.fq.gz
```

Core command pattern:

```bash
bwa mem -t "${THREADS}" \
  -R "@RG\tID:${sample}\tSM:${sample}\tPL:${PLATFORM:-illumina}\tLB:${sample}\tPU:${sample}" \
  "${REFERENCE_FASTA}" "${fq1}" "${fq2}" |
samtools sort -@ "${THREADS}" -m "${SORT_MEM:-1G}" \
  -o "${OUTDIR}/02.mapping/${sample}.sort.bam" -

picard MarkDuplicates \
  I="${OUTDIR}/02.mapping/${sample}.sort.bam" \
  O="${OUTDIR}/02.mapping/${sample}.sort.markdup.bam" \
  M="${OUTDIR}/02.mapping/${sample}.markdup.metrics.txt" \
  CREATE_INDEX=true \
  REMOVE_DUPLICATES=false
```

Build missing reference indexes only when needed:

```bash
samtools faidx "${REFERENCE_FASTA}"
bwa index "${REFERENCE_FASTA}"
picard CreateSequenceDictionary R="${REFERENCE_FASTA}" O="${REFERENCE_FASTA%.*}.dict"
```

## 输出结果

Expected files:

- `02.mapping/<sample>.sort.markdup.bam`
- `02.mapping/<sample>.sort.markdup.bam.bai`
- `02.mapping/<sample>.markdup.metrics.txt`
- `02.mapping/<sample>.flagstat.txt`
- `02.mapping/<sample>.coverage.txt`
- `logs/<sample>.bwa.log`
- `bam.list`, preferably `sample<TAB>bam_path`

Validation:

```bash
samtools quickcheck -v "${OUTDIR}/02.mapping/"*.bam
samtools view -H "${OUTDIR}/02.mapping/${sample}.sort.markdup.bam" | grep '^@RG'
samtools flagstat "${OUTDIR}/02.mapping/${sample}.sort.markdup.bam" > "${OUTDIR}/02.mapping/${sample}.flagstat.txt"
samtools coverage "${OUTDIR}/02.mapping/${sample}.sort.markdup.bam" > "${OUTDIR}/02.mapping/${sample}.coverage.txt"
```

Report mapped percentage, properly paired percentage, duplicate rate, mean depth, and failed samples.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| BAM has no `@RG` | GATK sample tracking will fail | rerun with `-R` or use Picard `AddOrReplaceReadGroups` |
| `.bai` missing | downstream random access fails | run `samtools index` |
| very low mapping rate | wrong reference, contamination, poor reads | inspect FASTQ QC, reference version, sample identity |
| duplicate rate extreme | PCR/library issue or duplicated input lanes | inspect lane table and duplication metrics |
| contig mismatch later | mapping reference differs from calling reference | use one reference path/version across all stages |

Do not remove duplicates by default for WGS variant calling.

## 相似的 skills

- `genomics/wgs-gatk-snp-indel` - high-confidence SNP/InDel calling from these BAMs.
- `genomics/wgs-bcftools-snp-indel` - fast SNP/InDel calling from these BAMs.
- `genomics/wgs-manta-sv` - default short-read SV from these BAMs.
- `genomics/wgs-lumpy-sv` - legacy LUMPY fallback from these BAMs.
- `genomics/wgs-cnvnator-cnv` - CNVnator read-depth CNV from these BAMs.
- `genomics/rnaseq-variant-calling` - use for RNA-seq reads, not this skill.
- `genomics/long-read-sv-sniffles2` - use for ONT/PacBio reads.
