# CERIS-JGRA GxE Install And Data SOP

Use this file when the user asks for CERIS-JGRA installation, R package setup, environmental data preparation, or exact reproduction. Formal analysis uses the upstream CERIS-JGRA R repository or a recorded R implementation of the same environmental-index and reaction-norm model.

This skill needs user multi-environment data or downloaded/paper-specific GxE data with recorded source, subset, and checksum information.

## Upstream Exact Reproduction

Primary source:

- Paper: Li et al., Molecular Plant 14(6), 874-887 (2021), DOI https://doi.org/10.1016/j.molp.2021.03.010
- Repository: https://github.com/jmyu/CERIS_JGRA

Create an R environment, clone the repository, and install packages required by the scripts after inspecting their `library(...)` calls:

```bash
git clone https://github.com/jmyu/CERIS_JGRA.git external/CERIS_JGRA
cd external/CERIS_JGRA
mamba create -n ceris_jgra -c conda-forge r-base r-data.table r-tidyverse r-ggplot2 r-lme4 r-mgcv r-readxl r-caret -y
conda run -n ceris_jgra Rscript -e "source('Sub_functions.r'); sessionInfo()"
```

Run the crop-specific folder or main `CERIS-JGRA.r` only after paths and crop example inputs are set:

```bash
conda run -n ceris_jgra Rscript CERIS-JGRA.r
```

Record repository commit, crop folder used (`0Maize`, `Wheat`, `Oat`, or custom), environmental variables, growth windows, reaction-norm model, and validation split.

## Model/Data Download

- No pretrained model is expected. The workflow derives environmental indices and trains GWAS/genomic prediction models from the user's multi-environment data.
- Required data: phenotype by genotype/environment, environmental time series by environment/date/variable, genotype markers, and growth-stage windows.
- For target environments, provide the same environmental variables and growth-window definitions used during training.
- Flag extrapolation when target environmental index values are outside the training range.
