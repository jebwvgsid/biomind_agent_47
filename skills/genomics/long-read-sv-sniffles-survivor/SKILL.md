---
name: long-read-sv-sniffles-survivor
description: Detect and merge structural variants from ONT, PacBio CLR, or PacBio HiFi long-read sequencing using minimap2, Sniffles, and optionally SURVIVOR. Use when users provide long-read FASTQ/BAM and ask for SV discovery, genotyping, or cohort SV merging. Do not use for short-read LUMPY SV, CNVnator CNV, or SNP/InDel calling.
version: "1.0.0"
tags: [long-read, ONT, PacBio, HiFi, SV, minimap2, Sniffles, SURVIVOR, structural-variant]
legacy_ids: [longread-sv, sniffles-sv, survivor-merge, nanopore-sv, pacbio-sv]
task_types: [long_read_sv_calling]
stage_tags: [long_read_mapping, sniffles, survivor_merge]
inputs: [reference_fasta, long_read_fastq, long_read_bam, platform]
outputs: [long_read_bam, sample_sv_vcf, merged_sv_vcf, sv_summary]
runtime:
  default_env: bio_longread_sv
  env_runner: bash
  language: bash
  packages: [minimap2, samtools, sniffles, survivor, bcftools]
  capabilities: [long-read-alignment, sv-calling, sv-merging]
  healthcheck: bash -lc "minimap2 --version >/dev/null && samtools --version >/dev/null && sniffles --version >/dev/null"
---

# Long-Read SV with Sniffles and SURVIVOR

Use this skill for long-read SV discovery and cohort merging. Always identify the platform first because minimap2 presets differ.

## 确认任务边界

Use this skill when:

- Input is ONT, PacBio CLR, or PacBio HiFi FASTQ/BAM.
- The goal is structural variants such as DEL, INS, DUP, INV, BND/TRA.
- The user wants per-sample SVs, genotyping, or cohort merging.

Do not use this skill when:

- Input is paired-end short-read WGS; use LUMPY.
- The goal is read-depth CNV; use CNVnator or another CNV caller.
- The goal is SNP/InDel small variants.

Accept existing BAMs only after confirming they are aligned to the same reference and indexed.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `REFERENCE_FASTA` | reference genome |
| `SAMPLE_TABLE` | `sample fastq platform` or `sample bam platform` |
| `PLATFORM` | `ont`, `pacbio`, `hifi`, or `pb` |
| `OUTDIR` | output directory |

Recommended:

| Parameter | Default / notes |
|---|---|
| `THREADS` | 8-32 |
| `MIN_SUPPORT` | start with auto/default; tune by depth |
| `MIN_SV_LEN` | 50 bp common starting point |
| `MERGE_DISTANCE` | 1000 bp common SURVIVOR starting point |

Minimap2 preset:

| Platform | Preset |
|---|---|
| ONT | `map-ont` |
| PacBio CLR | `map-pb` |
| PacBio HiFi | `map-hifi` |

Alignment:

```bash
minimap2 -x "${PRESET}" -t "${THREADS:-16}" --MD --cs -a \
  "${REFERENCE_FASTA}" "${FASTQ}" |
samtools sort -@ "${THREADS:-16}" \
  -o "${OUTDIR}/02.mapping/${sample}.sort.bam" -
samtools index "${OUTDIR}/02.mapping/${sample}.sort.bam"
```

Sniffles command selection:

- Prefer Sniffles2 syntax when `sniffles --help` shows `--input` and `--vcf`.
- Use legacy syntax only on old installations that require `-m` and `-v`.

Sniffles2-style discovery:

```bash
sniffles \
  --input "${OUTDIR}/02.mapping/${sample}.sort.bam" \
  --vcf "${OUTDIR}/03.variant_calling/${sample}.sniffles.vcf" \
  --threads "${THREADS:-8}" \
  --minsvlen "${MIN_SV_LEN:-50}"
```

Legacy Sniffles v1-style discovery:

```bash
sniffles \
  -m "${OUTDIR}/02.mapping/${sample}.sort.bam" \
  -v "${OUTDIR}/03.variant_calling/${sample}.sniffles.vcf" \
  --min_length "${MIN_SV_LEN:-50}" \
  --threads "${THREADS:-8}"
```

SURVIVOR merge:

```bash
find "${OUTDIR}/03.variant_calling" -name "*.sniffles.vcf" | sort > "${OUTDIR}/03.variant_calling/vcf_raw_calls.txt"
SURVIVOR merge "${OUTDIR}/03.variant_calling/vcf_raw_calls.txt" \
  "${MERGE_DISTANCE:-1000}" 1 1 -1 -1 -1 \
  "${OUTDIR}/04.filter/merged.sv.vcf"
```

For Sniffles2 multi-sample projects, consider SNF-based workflows if the installed version supports them; record exact syntax from `sniffles --help`.

## 输出结果

Expected outputs:

- `02.mapping/<sample>.sort.bam`
- `02.mapping/<sample>.sort.bam.bai`
- `03.variant_calling/<sample>.sniffles.vcf`
- `04.filter/<sample>.sniffles.filter.vcf`
- `04.filter/merged.sv.vcf`
- `05.summary/longread_sv_counts.tsv`
- `logs/*.log`

Validation:

```bash
samtools quickcheck -v "${OUTDIR}/02.mapping/"*.sort.bam
test -s "${OUTDIR}/04.filter/merged.sv.vcf"
grep -v '^#' "${OUTDIR}/04.filter/merged.sv.vcf" |
  awk -F'\t' '{print $8}' | tr ';' '\n' | grep '^SVTYPE=' | sort | uniq -c \
  > "${OUTDIR}/05.summary/longread_sv_counts.tsv"
```

Report platform, minimap2 preset, Sniffles version/syntax, merge distance, min support, and SV counts by type.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| wrong minimap2 preset | platform mismatch | choose `map-ont`, `map-pb`, or `map-hifi` correctly |
| Sniffles option error | v1/v2 syntax mismatch | inspect `sniffles --help` and switch syntax |
| few SVs | low depth, high support threshold, poor mapping | inspect depth/read length and tune support |
| overmerged cohort SVs | merge distance too permissive | reduce SURVIVOR distance |
| mixed references | cohort merge invalid | re-align to one reference |

## 相似的 skills

- `genomics/wgs-lumpy-sv` - short-read SV.
- `genomics/wgs-cnvnator-cnv` - read-depth CNV.
- `genomics/assembly-variant-syri` - assembly-based structural variation.
- `genomics/wgs-short-read-mapping` - short-read mapping, not long-read.
