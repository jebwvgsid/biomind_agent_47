---
name: modkit-pileup
description: 'Generate Oxford Nanopore 5mC/5hmC methylation bedMethyl, modBAM QC summary, probability-threshold
  QC, bigWig tracks, and optional plant CG/CHG/CHH context annotation from aligned, coordinate-sorted, indexed modBAM
  files with MM/ML tags using modkit. Use when the user asks for ONT 5mC methylation detection, modkit pileup, modBAM
  tag checks, bedMethyl generation, 5mC bigWig tracks, plant methylation contexts, CG/CHG/CHH annotation, or methylation
  benchmarking. Do not use for POD5 raw-signal basecalling or 6mA open-chromatin prediction.

  '
metadata:
  version: 1.1.0
compatibility: 需要本技能 references/dependencies.md 列明的现有软件；不自动安装。输入输出位于分析项目。
---


## 资源定位与依赖

使用当前 Agent 的标准技能发现与文件读取能力加载本文件。`scripts/`、`references/`、
`assets/` 相对本 SKILL.md 的实际目录；输入、输出和样本清单相对本次分析项目。
命令中的 `${SKILL_DIR}` 必须先设为实际技能目录的绝对路径；该变量不由宿主自动提供。
例如，从已加载位置解析目录后，以带引号的脚本绝对路径执行，保持工作目录为分析项目，
不要在技能安装目录写结果。跨技能资源按其实际目录定位；分享完整技能包时保留目录结构。
软件和参考数据库来自已有环境或用户显式提供的位置。依赖见
[dependencies.md](references/dependencies.md)；安装、升级或修改环境需要事先明确授权。
本文命令可由任意提供命令执行能力的 Agent 或终端使用；不会在读取技能时安装或体检环境。

# Nanopore Modkit 5mC Pileup

## Scope

Use this skill after modified-base basecalling has already produced an ONT modBAM. The starting point must be an aligned, coordinate-sorted BAM with valid `MM`/`ML` tags and a matching reference FASTA.

Route by input type:

| Input from user | Action |
|---|---|
| Aligned sorted indexed modBAM with `MM`/`ML` | Run this skill |
| Aligned BAM without `MM`/`ML` | Stop; this is not a modBAM |
| Unaligned modBAM | Align, sort, and index first while preserving tags, or use `dorado-modbase-prep` |
| POD5 / FAST5 / FASTQ only | Use `dorado-modbase-prep`; modkit does not call methylation from raw signal |
| Existing bedMethyl | Use downstream conversion, annotation, plotting, or DMR skills |
| 6mA accessibility / open chromatin | Use `skills/open-chromatin/modkit-cpu` |

Reference facts checked from upstream documentation:

- `modkit` is for Oxford Nanopore modified-base BAM data, especially converting modBAM to bedMethyl and producing summary statistics.
- GitHub latest release checked on 2026-06-13: `v0.6.4`, released 2026-06-11.
- For modkit `v0.6+`, prefer explicit `--modified-bases` with `--reference`.
- `--cpg` is an alias of `--motif CG 0`; do not use it by default for plant all-cytosine methylation.

Sources: https://github.com/nanoporetech/modkit, https://nanoporetech.github.io/modkit/intro_pileup.html, https://nanoporetech.github.io/modkit/intro_modbam_check_tags.html, https://nanoporetech.github.io/modkit/intro_bedmethyl_merge.html.

## Required Inputs

- `BAM`: aligned coordinate-sorted modBAM.
- `REFERENCE_FASTA`: reference genome FASTA matching BAM alignment.
- `SAMPLE`: sample name used for output prefixes.
- `OUTDIR`: output directory.
- `MODIFIED_BASES`: default `5mC`; use `5mC 5hmC` only when both should be tabulated.
- `THREADS`: default 8; increasing far beyond 8 may have little benefit for typical 40X human-scale data.
- Optional `REGION` or `INCLUDE_BED`.
- Optional `PLANT_CONTEXT=1` for CG/CHG/CHH annotation.

## Mandatory Preflight

Run these checks before `pileup`:

```bash
modkit --version
samtools quickcheck -v "${BAM}"
test -f "${BAM}.bai" || test -f "${BAM}.csi" || samtools index "${BAM}"
test -f "${REFERENCE_FASTA}.fai" || samtools faidx "${REFERENCE_FASTA}"
modkit modbam check-tags "${BAM}" --head 1000 --log "${OUTDIR}/logs/${SAMPLE}.check_tags.log" \
  > "${OUTDIR}/logs/${SAMPLE}.check_tags.txt"
```

Stop if `check-tags` fails or if the report does not contain the expected cytosine modification tag:

| Target | Expected SAM tag header / code |
|---|---|
| 5mC | `C+m` or mod code `m` |
| 5hmC | `C+h` or mod code `h` |

Do not silently continue with an ordinary BAM. Dorado or another modified-base caller must create `MM`/`ML` tags first.

## Standard 5mC Workflow

Create stable output folders:

```bash
mkdir -p "${OUTDIR}/bedmethyl" "${OUTDIR}/bigwig" "${OUTDIR}/qc" "${OUTDIR}/logs"
```

Summarize the modBAM:

```bash
modkit modbam summary "${BAM}" \
  --reference "${REFERENCE_FASTA}" \
  --threads "${THREADS:-8}" \
  > "${OUTDIR}/qc/${SAMPLE}.modkit_summary.tsv"
```

Collect probability threshold QC on a sample of reads when a quick check is enough:

```bash
modkit modbam sample-probs "${BAM}" \
  --num-reads 100000 \
  --threads "${THREADS:-8}" \
  > "${OUTDIR}/qc/${SAMPLE}.sample_probs.tsv"
```

Run all-cytosine plant-friendly 5mC pileup:

```bash
modkit pileup "${BAM}" "${OUTDIR}/bedmethyl/${SAMPLE}.5mC.bedmethyl.gz" \
  --modified-bases 5mC \
  --reference "${REFERENCE_FASTA}" \
  --bgzf \
  --threads "${THREADS:-8}" \
  --log-filepath "${OUTDIR}/logs/${SAMPLE}.pileup.log"
tabix -p bed "${OUTDIR}/bedmethyl/${SAMPLE}.5mC.bedmethyl.gz"
```

For mammalian CpG-only analysis, add `--cpg`. For a custom motif, use `--motif MOTIF OFFSET`. For target regions, add one of:

```bash
--region Chr01:100000-200000
--include-bed /path/to/regions.bed
```

`--include-bed` affects threshold estimation for `pileup`, `summary`, and `sample-probs`; record this in the final report.

## bigWig Track

Create a 5mC bigWig from bedMethyl:

```bash
modkit bedmethyl tobigwig \
  "${OUTDIR}/bedmethyl/${SAMPLE}.5mC.bedmethyl.gz" \
  "${OUTDIR}/bigwig/${SAMPLE}.5mC.bw" \
  --mod-code m \
  -g "${REFERENCE_FASTA}.fai" \
  --negative-strand-values \
  --log "${OUTDIR}/logs/${SAMPLE}.5mC_tobigwig.log"
```

Create separate tracks for 5hmC (`--mod-code h`) and 5mC (`--mod-code m`) when both are present. Do not merge 5mC and 5hmC unless the user explicitly asks for total cytosine modification.

## Plant CG/CHG/CHH Annotation

For plant and crop methylomes, do not default to CpG-only. First generate all-cytosine 5mC bedMethyl, then annotate strand-aware context:

```bash
python "${SKILL_DIR}/scripts/annotate_plant_context.py" \
  --bedmethyl "${OUTDIR}/bedmethyl/${SAMPLE}.5mC.bedmethyl.gz" \
  --reference "${REFERENCE_FASTA}" \
  --output "${OUTDIR}/bedmethyl/${SAMPLE}.5mC.CG_CHG_CHH.bedmethyl.tsv.gz"
```

The helper appends one column named `methylation_context` with `CG`, `CHG`, `CHH`, or `UNKNOWN`.

## bedMethyl Columns To Remember

For current modkit documentation, bedMethyl column 11 is `percent modified`, computed as `(N_mod / N_valid_cov) * 100`. Do not assume it is a 0-1 fraction unless the user gives a non-modkit table with a different schema.

Important columns:

| Column | Meaning |
|---|---|
| 4 | modified base code, often `m` for 5mC |
| 5 / 10 | valid coverage, `N_valid_cov` |
| 11 | percent modified |
| 12 | `N_mod` |
| 13 | `N_canonical` |
| 14 | `N_other_mod` |
| 16 | `N_fail`, calls below filter threshold |
| 18 | `N_nocall` |

## Validation

After running, verify:

```bash
test -s "${OUTDIR}/bedmethyl/${SAMPLE}.5mC.bedmethyl.gz"
test -s "${OUTDIR}/bedmethyl/${SAMPLE}.5mC.bedmethyl.gz.tbi"
gzip -cd "${OUTDIR}/bedmethyl/${SAMPLE}.5mC.bedmethyl.gz" | awk 'BEGIN{ok=1} !/^#/ && NF<18{ok=0; exit} END{exit ok?0:1}'
gzip -cd "${OUTDIR}/bedmethyl/${SAMPLE}.5mC.bedmethyl.gz" | awk '!/^#/ && $10>0 && $11>=0 && $11<=100 {found=1; exit} END{exit found?0:1}'
```

Report these in the final answer:

- modkit version.
- sample, BAM, reference, and output directory.
- whether `C+m` was found by `check-tags`.
- whether analysis was all-cytosine, CpG-only, motif-limited, region-limited, or BED-limited.
- bedMethyl, tabix index, summary TSV, pileup log, and bigWig paths.
- for plant output, counts of `CG`, `CHG`, `CHH`, and `UNKNOWN`.

## Common Failure Modes

| Symptom | Meaning | Fix |
|---|---|---|
| No `MM`/`ML` tags | Not a modified-base BAM | Re-run Dorado modified basecalling |
| No `C+m` tag | No 5mC calls in this BAM | Check model selection, e.g. all-context `5mC` vs motif `5mCG` |
| Empty bedMethyl | wrong reference, over-restrictive BED/region, or no usable calls | Check BAM header, FASTA, `check-tags`, and pileup log |
| Only CG sites reported for plant data | `--cpg` or CG-only Dorado model was used | For all-cytosine contexts, use all-context Dorado `5mC` and no `--cpg` |
| DMR fails on this bedMethyl | missing bgzip/tabix index or not from modkit pileup | Recompress/index and use `modkit-dmr` |
