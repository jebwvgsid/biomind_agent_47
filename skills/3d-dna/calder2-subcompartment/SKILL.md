---
name: calder2-subcompartment-analysis
description: >
  Run CALDER2 hierarchical compartment and sub-compartment calling from Hi-C,
  Micro-C, Pore-C, or other chromatin contact matrices through the BioMind
  CALDER2 wrapper. Use only when the user explicitly asks for CALDER2, Calder,
  hierarchical compartments, sub-compartments/subcompartments, 亚区室, 层级区室,
  A.1/A.2/B.1/B.2 labels, or Compartment4/Compartment8 outputs. Do not use
  this skill for a plain request to 鉴定区室, A/B区室鉴定, compartment calling, or
  ordinary A/B compartments. For ordinary A/B compartment calling, use
  3d-dna/compartment-calling.
metadata:
  version: "1.0.0"
  tags: [CALDER2, Calder, sub-compartment, subcompartment, hierarchical-compartment, Hi-C, Micro-C, Pore-C, 亚区室, 层级区室, A.1, A.2, B.1, B.2, Compartment4, Compartment8]
  task_types: [chromatin_contact_compartment]
  stage_tags: [subcompartment_calling]
  inputs: [cool_matrix, mcool_matrix, resolution, feature_track]
  outputs: [sub_compartment_tracks, compartment_summary]
  when_to_use:
    - User explicitly asks for CALDER2 or sub-compartment labels.
    - User needs hierarchical A/B sub-compartments such as A.1, A.2, B.1, B.2.
  when_not_to_use:
    - Ordinary A/B compartment calling where PC1/eigenvector output is enough.
    - Differential comparison from already existing compartment files.
  legacy_ids: [calder2-compartment, calder2-subcompartment, calder-subcompartment, sub-compartment, subcompartment-calling, 亚区室鉴定, 层级区室鉴定]
  runtime:
    default_env: calder2
    install_forbidden_envs: [hicexplore]
    env_runner: conda run -n calder2 python
    language: python
    packages: [cooler, pandas, numpy, scipy, h5py]
    optional_packages: [CALDER, data.table]
    install_tool: calder_r_runtime
    install_policy: declared-only
    install_command: conda create -n calder2 -c conda-forge -c bioconda python=3.10 r-calder2 cooler pandas numpy scipy h5py r-data.table -y
    capabilities: [calder2, calder, hierarchical-subcompartment, sub-compartment-summary, cooler-to-calder-dump]
    healthcheck: conda run -n calder2 python skills/3d-dna/calder2-subcompartment/scripts/run_calder_compartments.py --help
    mode_healthchecks:
      official-r:
        - conda run -n calder2 python -c "import cooler, pandas, numpy, h5py; print('PY_OK')"
        - conda run -n calder2 which Rscript
        - conda run -n calder2 Rscript -e "suppressPackageStartupMessages(library(CALDER)); suppressPackageStartupMessages(library(data.table)); cat('CALDER_OK\n')"
---

# CALDER2 Hierarchical Sub-Compartment Analysis

## Overview

Use this skill only for CALDER2 hierarchical compartment and sub-compartment
calling. CALDER2 is not the default A/B compartment caller in BioMind; ordinary
A/B calls should use `3d-dna/compartment-calling` with PC1/eigenvector analysis.

CALDER2's upstream interfaces are Juicer-compatible `.hic` files or
per-chromosome three-column dump contact tables. BioMind's wrapper supports
`.cool/.mcool` by exporting each selected chromosome to CALDER-compatible dump
tables before calling `CALDER()` from R. Do not report `.mcool` as native
upstream CALDER2 input.

## Runtime Design

Do not install CALDER2 into `hicexplore`. CALDER2 brings a full R/Bioconductor
stack (`r-calder2`, `GenomicRanges`, `data.table`, R version constraints, and
related dependencies), which is easy to conflict with an existing HiCExplorer
environment. Use the independent `calder2` environment:

```bash
conda create -n calder2 -c conda-forge -c bioconda python=3.10 r-calder2 cooler pandas numpy scipy h5py r-data.table -y
```

Do not use `r-calder`; that is not the CALDER2 package used here. Do not use
CRAN `install.packages("CALDER")` or `devtools::install_github(...)` as the
default repair path.

Mandatory healthchecks:

```bash
conda run -n calder2 python -c "import cooler, pandas, numpy, h5py; print('PY_OK')"
conda run -n calder2 which Rscript
conda run -n calder2 Rscript -e "suppressPackageStartupMessages(library(CALDER)); suppressPackageStartupMessages(library(data.table)); cat('CALDER_OK\n')"
```

Only proceed after all checks pass.

## Feature Track / Gene Density

For non-human/mouse genomes, run CALDER2 with `--genome others` and provide a
feature track positively associated with active/A compartments, such as binned
gene density, H3K27ac, or H3K4me3.

The feature track must be binned at the same resolution as the matrix:

```text
chrom  start  end  gene_count_or_density
```

Do not pass a raw gene BED as `gene_density.bed`. Generate or verify one row per
matrix bin, matching the selected cooler chromnames and bin size.

## Quick Start

Official CALDER2 mode from `.mcool` via wrapper dump conversion:

```bash
conda run -n calder2 python skills/3d-dna/calder2-subcompartment/scripts/run_calder_compartments.py \
  --sample leaf Matrix/Final/leaf.mcool \
  --sample bud Matrix/Final/bud.mcool \
  --resolution-bp 40000 \
  --feature-track ref/gene_density.bed \
  --calder-mode official-r \
  --genome others \
  --out-root Compartments/CALDER2 \
  --threads 4
```

Plant-style `Chr01` names when CALDER expects stripped chromosome IDs and a
`chr` feature-track prefix:

```bash
conda run -n calder2 python skills/3d-dna/calder2-subcompartment/scripts/run_calder_compartments.py \
  --sample leaf Matrix/Final/leaf.mcool \
  --resolution-bp 40000 \
  --feature-track ref/gene_density.bed \
  --genome others \
  --chr-prefix Chr \
  --strip-chr-prefix-for-calder \
  --feature-track-calder-prefix chr \
  --restore-from-prefix chr \
  --restore-output-prefix Chr \
  --out-root Compartments/CALDER2 \
  --threads 4
```

## Outputs

The wrapper normalizes CALDER2 outputs to:

```text
sample  chrom  start  end  level  compartment  resolution_bp
```

`Compartment2` is A/B, `Compartment4` is a two-level label such as `A.1`, and
`Compartment8` is a deeper sub-compartment label such as `A.1.1`.

## Related Skills

- `3d-dna/compartment-calling` - default PC1/eigenvector A/B calling.
- `3d-dna/differential-compartment` - compare A/B or sub-compartment labels
  after tracks exist.
