---
name: compartment-calling
description: 'Call A/B compartments from Hi-C, Micro-C, Pore-C, or other .cool/.mcool chromatin contact matrices
  using PC1/eigenvector analysis by default. This is the default BioMind skill for ordinary 区室鉴定 / A/B区室鉴定 / compartment
  calling. Use when the user asks to identify/call compartments, calculate A/B compartments, run PC1/eigenvector
  compartment interpretation, orient compartments with gene density, or produce per-sample A/B compartment tracks
  from .cool/.mcool matrices, unless the user explicitly asks for CALDER2, hierarchical compartments, sub-compartments,
  or A.1/A.2/B.1/B.2 labels. For those explicit CALDER2/sub-compartment requests, use 3d-dna/calder2-subcompartment
  instead.

  '
metadata:
  version: 1.1.1
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

# A/B Compartment Analysis With PC1/Eigenvector

## Overview

Use this skill to call ordinary A/B compartments from `.cool` or `.mcool`
contact matrices. The default method is cis eigenvector/PC1 compartment calling
with `cooltools.eigs_cis`-style logic. A binned feature track such as gene
density can be used as the phasing track so the A/B sign is biologically
oriented instead of arbitrary.

Do not use CALDER2 as the default A/B caller. CALDER2 is reserved for explicit
hierarchical or sub-compartment requests such as `A.1`, `A.2`, `B.1`, `B.2`, or
`Compartment4/Compartment8`. For those requests use
`skills/3d-dna/calder2-subcompartment`.

## Inputs

Minimum inputs:

- One or more `.cool` or `.mcool` matrix paths.
- Explicit resolution in bp for `.mcool` inputs, for example `40000`.
- Optional but recommended feature track BED for A/B sign orientation, commonly
  binned gene density.
- If gene density must be generated: reference FASTA plus gene annotation
  GFF3/GTF.

Do not guess `.mcool` resolution. If the user gives plain `.mcool` paths and no
resolution, list available resolutions with `cooler` and ask for confirmation.

When inspecting `.cool/.mcool` chromosome names or sizes, do not invent cooler
CLI subcommands. Prefer the Python API inside the declared runtime:

```bash
python -c '
import cooler
uri = "Matrix/Final/leaf.mcool::/resolutions/40000"
clr = cooler.Cooler(uri)
for chrom, size in clr.chromsizes.items():
    print(f"{chrom}\t{int(size)}")
'
```

Matrix preflight must be explicit enough to catch subset matrices:

- Write the full `chromnames` and `chromsizes` for every input matrix and
  resolution, not just the intersection or a summarized statement.
- If FASTA/GFF3 are provided, derive their primary chromosome names after the
  documented name harmonization and compare them against the cooler
  `chromnames`.
- Do not treat "leaf and bud have matching chromosomes" as sufficient
  validation. Matching can still mean both matrices are the same subset.
- If the cooler matrix contains only a small subset of expected chromosomes
  such as `Chr03, Chr04, Chr06`, stop and report that the input `.mcool` appears
  to be a chromosome subset unless the user explicitly requested those
  chromosomes.
- Do not hard-code chromosome mappings from the observed cooler subset. Generate
  mappings from the full FASTA/GFF3 chromosome set, then mark which mapped
  chromosomes are present or absent in each cooler matrix.

## Feature Track / Gene Density Preflight

When the user asks to extract or generate gene density from FASTA/GFF3, do not
invent an ad-hoc BED file. A valid compartment orientation feature track must be
a binned track at the same resolution as the matrix, with one row per genomic
bin:

```text
chrom  start  end  gene_count_or_density
```

Hard requirements:

- Derive the chromosome set from the selected cooler URI, for example
  `leaf.mcool::/resolutions/40000`, not from assumptions.
- Derive chromosome lengths from real data: preferably
  `cooler.Cooler(uri).chromsizes` from the Python API, or FASTA `.fai` after
  documented name harmonization.
- Harmonize names with explicit prefix rewriting. For example, if FASTA/GFF3
  uses `Ca_59Chr03` and cooler uses `Chr03`, remove only the exact `Ca_59`
  prefix:

```awk
chr = $1
sub(/^Ca_59/, "", chr)
```

- Convert GFF3 gene intervals to BED correctly: GFF3 start is 1-based
  inclusive, so BED start is `start - 1` and BED end is `end`.
- Filter empty and malformed BED rows before sorting/intersection:

```bash
awk 'NF>=3 && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ && $3>$2'
```

- If `bedtools` is available inside the active runtime, use
  `bedtools makewindows` and `bedtools intersect -c`. If not, use a
  deterministic Python implementation that writes the same four-column BED.

Minimum validation gates:

- Feature BED exists and is non-empty.
- Chromosome names exactly match the selected cooler chromnames.
- Bin width matches `--resolution-bp`, except possibly final chromosome bins.
- Column 4 numeric sum is greater than 0.
- A `chrom_name_mapping.tsv` or report documents any rewrite such as
  `Ca_59Chr03 -> Chr03`.
- A/B direction is validated by joining `ab_compartments.bed` or
  `eigenvectors.tsv` to `gene_density.bed` on `chrom`, `start`, and `end`, then
  comparing the mean gene density of A bins versus B bins.

Do not validate A/B direction with `paste ab_compartments.bed gene_density.bed`
or fixed `awk` field numbers such as `$8`. `ab_compartments.bed` may contain
more than four columns, so fixed fields can read the gene-density `end`
coordinate instead of column 4 gene count. Always use a keyed merge on
`chrom/start/end`.

Do not call a gene interval BED or a BED with gene lengths in column 4
`gene_density.bed`. That is an annotation interval track, not binned density.

## Runtime Preflight

Verify the PC1 runtime before execution:

```bash
python -c "import cooler, cooltools, pandas, numpy, h5py; print('PC1_OK')"
python "${SKILL_DIR}/scripts/call_ab_compartments_pc1.py" --help
```

`bedtools` is a CLI dependency, not a required Python module for this skill.
Do not use `import pybedtools` as a healthcheck unless the user explicitly asks
for a pybedtools-based implementation. If the CLI needs to be checked directly,
use `bedtools --version` or the wrapper healthcheck
above.

If the `compartment` environment is missing, repair it with the declared runtime
command:

```bash
conda create -n compartment -c conda-forge -c bioconda python=3.10 cooler cooltools pandas numpy scipy h5py bedtools -y
```

Do not install CALDER2 into this runtime. CALDER2 belongs to the optional
`3d-dna/calder2-subcompartment` skill and its independent `calder2` environment.

## Quick Start

Call A/B compartments for two samples from `.mcool` matrices with an oriented
gene-density feature track:

```bash
python "${SKILL_DIR}/scripts/call_ab_compartments_pc1.py" \
  --sample leaf Matrix/Final/leaf.mcool \
  --sample bud Matrix/Final/bud.mcool \
  --resolution-bp 40000 \
  --phasing-track ref/gene_density.bed \
  --out-root Compartments/PC1_AB \
  --n-eigs 1
```

If no feature track is available, PC1 sign orientation may be arbitrary. Report
that limitation explicitly.

The wrapper writes `orientation_validation.tsv` for each sample when
`--phasing-track` is supplied. Treat the run as failed if A bins do not have a
higher mean phasing value than B bins. Only valid A/B bins enter that comparison;
the `NA` row is diagnostic only. If either A or B has no valid bins, report that
direction validation could not be performed instead of claiming it passed.

## Outputs

For `--out-root Compartments/PC1_AB --resolution-bp 40000`:

```text
Compartments/PC1_AB/40000/
├── leaf/
│   ├── eigenvectors.tsv
│   ├── ab_compartments.bed
│   └── pc1_run_summary.tsv
├── bud/
├── compartments_long.tsv
├── compartment_length_summary.tsv
└── compartment_run_summary.tsv
```

`compartments_long.tsv` normalizes A/B calls to:

```text
sample  chrom  start  end  score  compartment  resolution_bp
```

Finite oriented PC1 values >= 0 are reported as `A`; finite values < 0 are
reported as `B`. Missing or non-finite PC1 values (`NaN`, `+Inf`, `-Inf`) must be
reported as the literal compartment label `NA`, never coerced to `A` or `B`.
Keep those bins and their original coordinates and scores in `eigenvectors.tsv`,
`ab_compartments.bed`, and `compartments_long.tsv`.

`compartment_length_summary.tsv` counts `NA` bins and their lengths separately.
Report invalid-bin counts; exclude `NA` from A/B proportions, direction checks,
and A/B switching comparisons. An all-`NA` result has no callable A/B bins and
must not be described as successful compartment identification. When reading
these tables with pandas, preserve the literal `NA` label (for example, use
`keep_default_na=False` and convert the numeric score column separately).

If a phasing track is not supplied or cannot orient the sign, report that A/B
labels may be sign-flipped.

## Related Skills

- `3d-dna/calder2-subcompartment` - CALDER2 hierarchical sub-compartments.
- `3d-dna/differential-compartment` - compare A/B or sub-compartment labels
  after compartment tracks exist.
