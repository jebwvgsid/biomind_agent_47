---
name: calder2-subcompartment
description: 'Run CALDER2 hierarchical compartment and sub-compartment calling from Hi-C, Micro-C, Pore-C, or other
  chromatin contact matrices through the BioMind CALDER2 wrapper. Use only when the user explicitly asks for CALDER2,
  Calder, hierarchical compartments, sub-compartments/subcompartments, 亚区室, 层级区室, A.1/A.2/B.1/B.2 labels, or Compartment4/Compartment8
  outputs. Do not use this skill for a plain request to 鉴定区室, A/B区室鉴定, compartment calling, or ordinary A/B compartments.
  For ordinary A/B compartment calling, use 3d-dna/compartment-calling.

  '
metadata:
  version: 1.0.0
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
python -c "import cooler, pandas, numpy, h5py; print('PY_OK')"
which Rscript
Rscript -e "suppressPackageStartupMessages(library(CALDER)); suppressPackageStartupMessages(library(data.table)); cat('CALDER_OK\n')"
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
python "${SKILL_DIR}/scripts/run_calder_compartments.py" \
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
python "${SKILL_DIR}/scripts/run_calder_compartments.py" \
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
