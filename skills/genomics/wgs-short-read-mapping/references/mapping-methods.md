# Mapping methods and examples (read for the current phase)
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

For a bounded prefix inspection, use the supplied standard-library Python scripts.
Resolve SKILL_DIR from the actual Skill location and keep input paths in the project:

```bash
python "${SKILL_DIR}/scripts/inspect_fastq.py" --path "${FASTQ}"
python "${SKILL_DIR}/scripts/inspect_fasta_header.py" --path "${REFERENCE_FASTA}"
python "${SKILL_DIR}/scripts/inspect_wgs_inputs.py" \
  --fastq sample_R1 "${FASTQ_R1}" --fastq sample_R2 "${FASTQ_R2}" \
  --reference reference "${REFERENCE_FASTA}" --evidence-dir "${OUTDIR}/input-inspection"
```

The mixed helper writes a **new** project evidence directory; an existing path is
refused. Labels and paths are separate arguments, including paths with spaces or
Chinese characters. `inspect_fastq_batch.py` provides the FASTQ-only equivalent
with repeated `--item LABEL PATH`. Do not put FASTA into the FASTQ-only reader.

Results are ordinary JSON, with per-item stdout/stderr and a summary for batches.
No model observation handle is required. A prefix never certifies full gzip
integrity, all reads, or mate pairing. The existing FASTQ defaults remain 32
records, 1 MiB compressed/input bytes, 65536 decoded bytes and 2 seconds.
`inspect_fasta_header.py` checks a plain FASTA header and one sequence byte, with
4096 header bytes, 8192 input bytes and 2 seconds; gzip FASTA is unsupported.
Over-limit, decoding and technical errors remain incomplete results.

Batch checks use a 60-second per-item deadline (explicitly adjustable with
`--item-timeout-seconds`). A timed-out check retains raw output and marks remaining
checks unstarted. No retry or long scientific computation is performed by these
inspection scripts. Inspect each result; return code alone is not a scientific
success claim. Batch output does not validate whole files.

Do not implement nonempty FASTQ detection as `zcat | head | grep -q` under
`pipefail`: early downstream termination can cause SIGPIPE after a valid header
was found. Do not ignore every 141 or disable pipefail. Empty/invalid observed
data, unsupported representation, decoding errors, and resource limits have
different meanings. Reader failures must retain their technical errors.

Unknown ploidy need not block these mapping/input checks. Carry it as an
unresolved user-owned decision to the calling Skill and an explicit question to the user; a mapping receipt or general execution approval cannot confirm
ploidy or validate downstream genotype filters.

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
| `ALIGNER` | explicitly selected, verified compatible implementation | keep its binding and command in the approved action |
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

## Scoped diagnostics

For an authorized BAM header diagnostic use `samtools view -H "${BAM}"`; for a
bounded binary prefix use an appropriate bounded file tool. Record exact paths,
read scope, return codes and errors. Larger reads require agreement on scope;
unsupported readers do not prove corrupt or missing data. Diagnostic evidence
cannot replace the scientific result checks above.
