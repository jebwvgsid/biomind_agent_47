---
name: assembly-variant-syri
description: Detect synteny-aware variants between two chromosome-level assemblies using MUMmer/nucmer alignment and SyRI, with optional plotsr visualization. Use when users provide reference and query assemblies and ask for assembly-based SNP, InDel, inversion, translocation, duplication, syntenic regions, or structural variation. Do not use on FASTQ/BAM read data.
version: "1.0.0"
tags: [assembly, SyRI, MUMmer, nucmer, delta-filter, structural-variant, synteny, inversion, translocation, plotsr]
legacy_ids: [syri-assembly-variant, assembly-sv, genome-alignment-variant, SyRI比较, SyRI倒位易位SNP, 两个染色体级组装用SyRI比较倒位易位和SNP, 组装比较SyRI]
task_types: [assembly_variant_calling]
stage_tags: [whole_genome_alignment, syri, synteny_sv]
inputs: [reference_assembly_fasta, query_assembly_fasta]
outputs: [delta_alignment, coords, syri_out, syri_vcf, syri_summary, plotsr_pdf]
runtime:
  default_env: bio_syri
  env_runner: bash
  language: bash
  packages: [mummer4, syri, plotsr, samtools]
  capabilities: [assembly-comparison, synteny-aware-sv, variant-vcf]
  healthcheck: bash -lc "nucmer --version >/dev/null && delta-filter -h >/dev/null 2>&1 && syri --help >/dev/null 2>&1"
  install_policy: prefer-declared
  install_command: mamba create -n bio_syri -c conda-forge -c bioconda -y syri mummer4 plotsr samtools
---

# Assembly Variant Calling with SyRI

Use this as the default assembly-level SV/synteny skill for pairwise comparison of chromosome-level assemblies. SyRI interpretation depends strongly on assembly contiguity and chromosome correspondence. Assemblytics and SVIM-asm are optional supplemental callers, not default stages.

## 确认任务边界

Use this skill when:

- The user provides exactly two assemblies: reference and query.
- Assemblies are chromosome-level or close enough for synteny interpretation.
- The goal is assembly-based SNP, InDel, inversion, translocation, duplication, or syntenic/non-syntenic region detection.

Do not use this skill when:

- Input is raw FASTQ/BAM.
- Assemblies are highly fragmented scaffolds and the user expects chromosome-scale SV conclusions.
- The two FASTA files are the same assembly.

By default, exclude organellar genomes, unplaced scaffolds, and very small contigs unless the user requests them.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `REFERENCE_ASSEMBLY` | reference FASTA |
| `QUERY_ASSEMBLY` | query FASTA |
| `OUTDIR` | output directory |

Recommended:

| Parameter | Default / notes |
|---|---|
| `MIN_CLUSTER` | 100 for small genomes; increase for large repeats |
| `BREAK_LENGTH` | 500 |
| `MIN_EXACT` | 50 |
| `IDENTITY` | 90 starting point; tune by divergence |
| `MIN_ALIGN_LEN` | 100 or higher for large genomes |

Preflight:

```bash
nucmer --version
delta-filter -h
show-coords -h
syri --help
samtools faidx "${REFERENCE_ASSEMBLY}"
samtools faidx "${QUERY_ASSEMBLY}"
```

Whole-genome alignment:

```bash
mkdir -p "${OUTDIR}/03.variant_calling" "${OUTDIR}/04.filter" "${OUTDIR}/05.summary" "${OUTDIR}/logs"
nucmer --maxmatch \
  -c "${MIN_CLUSTER:-100}" \
  -b "${BREAK_LENGTH:-500}" \
  -l "${MIN_EXACT:-50}" \
  -p "${OUTDIR}/03.variant_calling/out" \
  "${REFERENCE_ASSEMBLY}" "${QUERY_ASSEMBLY}" \
  > "${OUTDIR}/logs/nucmer.log" 2>&1
```

Filter and convert:

```bash
delta-filter -m \
  -i "${IDENTITY:-90}" \
  -l "${MIN_ALIGN_LEN:-100}" \
  "${OUTDIR}/03.variant_calling/out.delta" \
  > "${OUTDIR}/03.variant_calling/out.filtered.delta"

show-coords -T -H -r -d \
  "${OUTDIR}/03.variant_calling/out.filtered.delta" \
  > "${OUTDIR}/03.variant_calling/out.filtered.coords"
```

Run SyRI:

```bash
syri \
  -c "${OUTDIR}/03.variant_calling/out.filtered.coords" \
  -d "${OUTDIR}/03.variant_calling/out.filtered.delta" \
  -r "${REFERENCE_ASSEMBLY}" \
  -q "${QUERY_ASSEMBLY}" \
  --prefix "${OUTDIR}/04.filter/syri" \
  > "${OUTDIR}/logs/syri.log" 2>&1
```

If `syri --help` shows different prefix/output options, adapt and record the exact command.

Optional plot:

```bash
plotsr \
  "${OUTDIR}/04.filter/syri.out" \
  "${REFERENCE_ASSEMBLY}" \
  "${QUERY_ASSEMBLY}" \
  -H 8 -W 5 \
  -o "${OUTDIR}/05.summary/syri.pdf"
```

## 输出结果

Expected outputs:

- `03.variant_calling/out.delta`
- `03.variant_calling/out.filtered.delta`
- `03.variant_calling/out.filtered.coords`
- `04.filter/syri.out`
- `04.filter/syri.vcf`
- `04.filter/syri.summary`
- `05.summary/syri_variant_counts.tsv`
- optional `05.summary/syri.pdf`
- `logs/nucmer.log`, `logs/syri.log`

Validation:

```bash
test -s "${OUTDIR}/03.variant_calling/out.filtered.coords"
test -s "${OUTDIR}/04.filter/syri.out"
test -s "${OUTDIR}/04.filter/syri.vcf"
awk '{print $11}' "${OUTDIR}/04.filter/syri.out" | sort | uniq -c \
  > "${OUTDIR}/05.summary/syri_variant_counts.tsv"
```

Report assembly names, N50/contig level if known, alignment filtering thresholds, and variant category counts.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| no filtered alignments | thresholds too strict or wrong files | lower filters or verify assemblies |
| many tiny chaotic SVs | fragmented assemblies/repeats | report preliminary and filter to chromosome-level contigs |
| organelle/unplaced contigs dominate | wrong FASTA scope | build chromosome-only FASTA |
| SyRI coordinate errors | coords/delta generated with incompatible options | regenerate with documented `show-coords` flags |
| plotsr syntax differs | version mismatch | inspect `plotsr --help` and record command |

## 相似的 skills

- `genomics/assembly-variant-assemblytics` - optional delta-based assembly SV summary.
- `genomics/assembly-variant-svim-asm` - optional assembly-alignment SV caller.
- `genomics/long-read-sv-sniffles2` - read-based long-read SV.
- `genomics/wgs-manta-sv` - short-read SV.
- `genomics/wgs-cnvnator-cnv` - read-depth CNV.
- `genomics/assembly` - genome assembly before comparison.
