---
name: 3d-dna-analysis
description: Route and plan three-dimensional genome analysis skills for Hi-C, Micro-C, Pore-C, chromatin contact
  matrices, TAD calling, TAD boundary comparison, differential TAD testing, A/B compartments, loops, contact maps,
  and multi-sample 3D genome interpretation. Use as the entry skill when the user asks for 三维基因组, 3D genome, 3D
  DNA, Hi-C analysis, chromatin conformation, TAD, compartment, loop, or contact-matrix workflows; delegate execution
  to the specific 3d-dna subskill.
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

# 3D DNA / Three-Dimensional Genome Analysis

Use this skill as the top-level router for chromatin conformation and 3D genome tasks. It should choose the most specific subskill under `skills/3d-dna` and avoid mixing unrelated epigenomics workflows such as methylation or open-chromatin prediction unless the user explicitly asks for integration.

## Scope

- Hi-C, Micro-C, Pore-C, and other chromatin contact matrix workflows.
- `.cool`, `.mcool`, HiCExplorer, cooler, and resolution-aware matrix analysis.
- TAD calling, TAD boundary statistics, differential TAD testing, and downstream interpretation.
- A/B compartment, loop, contact-map, insulation score, and matrix QC planning when corresponding skills or tools are available.
- Multi-sample comparison across tissues, genotypes, treatments, or developmental stages.

Do not use this skill for 3D-DNA genome scaffolding/assembly polishing unless the user explicitly means the 3D-DNA scaffolding tool. For genome assembly scaffolding, use assembly or Hi-C scaffolding skills instead.

## Subskill Routing

| User need | Use |
|---|---|
| Check `.cool` / `.mcool` resolutions and existing balance weights only | `skills/3d-dna/tad-calling` read-only `check_mcool_balance.py` preflight |
| Call TADs from `.cool` or `.mcool` matrices | `skills/3d-dna/tad-calling` |
| Compare existing TAD boundaries across samples | `skills/3d-dna/tad-boundary-change` |
| Test differential TADs between target and control matrices | `skills/3d-dna/differential-tad` |
| Call ordinary A/B compartments from `.cool` / `.mcool` matrices | `skills/3d-dna/compartment-calling` |
| Call CALDER2 hierarchical sub-compartments from `.cool` / `.mcool` matrices | `skills/3d-dna/calder2-subcompartment` |
| Compare A/B or sub-compartment labels between samples | `skills/3d-dna/differential-compartment` |
| Call loops or differential loops from `.cool` / `.mcool` matrices | `skills/3d-dna/loop-calling` |
| Classify loop anchors with histone marks or other BED annotations | `skills/3d-dna/loop-classification` |
| Need sequence/gene annotation for TAD or loop regions | combine with `skills/databases/ensembl/ensembl-database` or local GFF/BED tools |
| Need methylation/expression tracks around genes | combine with `skills/epigenomics/methylation/gene-plot` |
| Need chromatin accessibility from Nanopore 6mA modBAM | use `skills/open-chromatin/modkit-cpu` |

## Planning Workflow

1. Identify the biological question: TAD detection, boundary change, differential TAD, compartment, loop, visualization, or multi-omics integration.
2. Confirm input type: `.cool`, `.mcool`, matrix URI with resolution, TAD BED files, boundary BED files, compartment BED files, or sample sheet.
3. Confirm sample design: one sample, pairwise target/control, time series, tissue panel, genotype comparison, or treatment comparison.
4. Confirm resolution explicitly for `.mcool` inputs; do not guess common values such as 40000.
5. Select the subskill and keep outputs organized by resolution and analysis type.
6. Report exact input paths, resolution, parameters, output directories, and interpretation limits.

## Output Organization

Prefer stable output roots:

```text
TADs/HiCExplorer/{resolution}/
TADs/HiCExplorer/{resolution}/boundary_changes/
TADs/HiCExplorer/{resolution}/differential_tads/
Compartments/{method}/{resolution}/
Loops/{method}/{resolution}/
Loops/Classification/{resolution}/
Figures/3d_dna/
```

Keep multi-sample tables long-form with `sample`, `condition`, `matrix`, `resolution_bp`, `chrom`, `start`, `end`, and method-specific metrics.

## Interpretation Rules

- TAD, compartment, and loop results are resolution- and normalization-dependent; always report matrix resolution and balancing status.
- `cooler.fileops.list_coolers()` only proves which resolution groups exist in an `.mcool`; it does not prove balance. Balance status must come from the bins-table weight column on each effective URI.
- Do not compare matrices at different resolutions unless the method explicitly supports it.
- Loop classification requires loop calls and BED annotations on the same genome assembly and chromosome naming convention.
- Differential TAD testing requires clear target/control direction; do not infer direction from file order.
- Boundary change summaries require a boundary tolerance policy tied to resolution.
- 3D genome features are not gene function annotations by themselves; connect them to genes, expression, methylation, accessibility, or QTL evidence only when those data are available.

## Related Skills

- `skills/3d-dna/tad-calling` - TAD calling with HiCExplorer `hicFindTADs`.
- `skills/3d-dna/tad-boundary-change` - Boundary gain/loss, shared, and shifted boundary summaries.
- `skills/3d-dna/differential-tad` - `hicDifferentialTAD` target/control testing.
- `skills/3d-dna/compartment-calling` - default PC1/eigenvector A/B compartment calling.
- `skills/3d-dna/calder2-subcompartment` - CALDER2 hierarchical sub-compartment calling.
- `skills/3d-dna/differential-compartment` - compartment switch and transition summaries.
- `skills/3d-dna/loop-calling` - loop and differential loop calling with Mustache.
- `skills/3d-dna/loop-classification` - BED mark-based loop anchor classification.
- `skills/open-chromatin/modkit-cpu` - Nanopore modkit open-chromatin prediction.
- `skills/epigenomics/methylation/gene-plot` - Gene-centered methylation/expression/compartment visualization.
