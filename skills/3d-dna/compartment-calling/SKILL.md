---
name: compartment-analysis
description: >
  Call A/B compartments from Hi-C, Micro-C, Pore-C, or other .cool/.mcool
  chromatin contact matrices using PC1/eigenvector analysis by default. This
  is the default BioMind skill for ordinary 区室鉴定 / A/B区室鉴定 /
  compartment calling. Use when the user asks to identify/call compartments,
  calculate A/B compartments, run PC1/eigenvector compartment interpretation,
  orient compartments with gene density, or produce per-sample A/B compartment
  tracks from .cool/.mcool matrices, unless the user explicitly asks for
  CALDER2, hierarchical compartments, sub-compartments, or A.1/A.2/B.1/B.2
  labels. For those explicit CALDER2/sub-compartment requests, use
  3d-dna/calder2-subcompartment instead.
metadata:
  version: "1.1.0"
  tags: [compartment, A/B-compartment, PC1, eigenvector, Hi-C, Micro-C, Pore-C, mcool, cool, gene-density, 区室分析, AB区室]
  task_types: [chromatin_contact_compartment]
  stage_tags: [compartment_calling]
  inputs: [cool_matrix, mcool_matrix, resolution, feature_track]
  outputs: [ab_compartment_tracks, eigenvector_tracks, compartment_summary]
  when_to_use:
    - Workflow stage is compartment_calling.
    - User needs A/B compartment calls from contact matrices.
    - User does not explicitly require CALDER2 hierarchical sub-compartments.
  when_not_to_use:
    - CALDER2 hierarchical sub-compartment calling.
    - Differential compartment summary from existing compartment files only.
  legacy_ids: [ab-compartment, compartment-calling, AB区室鉴定, 区室鉴定, 普通区室鉴定, 默认区室鉴定, A/B区室]
  runtime:
    default_env: compartment
    env_runner: conda run -n compartment python
    language: python
    packages: [cooler, cooltools, pandas, numpy, scipy, h5py, bedtools]
    install_tool: compartment_pc1_runtime
    install_policy: declared-only
    install_command: conda create -n compartment -c conda-forge -c bioconda python=3.10 cooler cooltools pandas numpy scipy h5py bedtools -y
    capabilities: [ab-compartment, pc1-eigenvector, cooler-preflight, feature-track-orientation]
    healthcheck: conda run -n compartment python skills/3d-dna/compartment-calling/scripts/call_ab_compartments_pc1.py --help
---

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
conda run -n compartment python -c '
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
conda run -n compartment python -c "import cooler, cooltools, pandas, numpy, h5py; print('PC1_OK')"
conda run -n compartment python skills/3d-dna/compartment-calling/scripts/call_ab_compartments_pc1.py --help
```

`bedtools` is a CLI dependency, not a required Python module for this skill.
Do not use `import pybedtools` as a healthcheck unless the user explicitly asks
for a pybedtools-based implementation. If the CLI needs to be checked directly,
use `conda run -n compartment bedtools --version` or the wrapper healthcheck
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
conda run -n compartment python skills/3d-dna/compartment-calling/scripts/call_ab_compartments_pc1.py \
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
higher mean phasing value than B bins.

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

Positive oriented PC1 is reported as `A`; negative oriented PC1 is reported as
`B`. If a phasing track is not supplied or cannot orient the sign, report that
A/B labels may be sign-flipped.

## Related Skills

- `3d-dna/calder2-subcompartment` - CALDER2 hierarchical sub-compartments.
- `3d-dna/differential-compartment` - compare A/B or sub-compartment labels
  after compartment tracks exist.
