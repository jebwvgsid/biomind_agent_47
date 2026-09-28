---
name: aporec-methylation-workflow
description: 'Route AporeC/Pore-C and ordinary Oxford Nanopore long-read methylation projects before execution.
  Use when the user has AporeC, Pore-C, nanopore modBAM, Dorado modified-base BAM, MM/ML tags, modkit methylation
  calls, or a Snakemake workflow such as porec.smk and needs a plan that separates third-generation direct methylation
  from second-generation bisulfite or enzymatic methyl-seq data.

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

# AporeC / Pore-C Methylation Workflow

Use this skill as the top-level router. Its job is to decide whether the user
has a long-read direct methylation project, a Pore-C/AporeC contact-plus-
methylation project, or a second-generation methylation project that must be
routed away from modkit.

This skill was written from the local Snakemake pattern in
用户现有 Pore-C 工作流 `porec.smk`（使用用户提供或环境注册表确认的实际路径）: `data/*.bam` inputs,
`config.yaml`, Dorado alignment, modkit pileup, optional Pore-C pairs/cool
generation, bigWig tracks, and gene-centered deepTools profiles.

## Decision Tree

Classify the data before selecting tools:

| Data type | Use this workflow? | Route |
|---|---:|---|
| Lab AporeC BAM with `MM`/`ML` tags | yes | full AporeC/Pore-C methylation plus contact workflow |
| ONT Pore-C BAM or POD5 | yes | Dorado prep if needed, then modkit plus Pore-C contact stage |
| Ordinary ONT WGS modBAM | partial | modkit methylation only; skip pairs/cool contact outputs |
| ONT BAM without `MM`/`ML` tags | blocked unless raw signal exists | use Dorado modified-base basecalling from POD5/FAST5 or stop |
| PacBio HiFi/CLR methylation | not by default | use PacBio-specific methylation callers or convert compatible outputs downstream |
| WGBS, EM-seq, RRBS, oxBS, TAB-seq | no | use short-read bisulfite/enzymatic methylation workflow, not modkit |
| Existing modkit bedMethyl | downstream only | use DMR, context annotation, bigWig, plotting skills |

Do not treat all "nanopore" data as Pore-C. Plain ONT WGS can produce
methylation tracks, but it cannot produce concatemer order, `.pairs.gz`, or
`.cool` contact matrices unless the library is Pore-C/AporeC or another
proximity-ligation long-read assay.

## Required Inputs

Collect these fields for a concrete execution plan:

| Field | Meaning |
|---|---|
| `ASSAY_TYPE` | `aporec`, `pore-c`, `ont-wgs`, `pacbio`, `wgbs`, `em-seq`, or other |
| `INPUT_KIND` | `pod5`, `fast5`, unaligned modBAM, aligned modBAM, or ordinary BAM |
| `REFERENCE_FASTA` | genome FASTA used for alignment and motif/context annotation |
| `SAMPLE_TABLE` | sample name and BAM/raw-read paths |
| `MODBASE` | `C`, `CG`, `GC`, or `A` following the local Snakemake convention |
| `GENE_BED` | BED/GTF-derived regions for deepTools profile plots |
| `BINSIZES` | Pore-C contact matrix resolutions, only for AporeC/Pore-C |
| `THREADS` and `OUTDIR` | compute resources and output root |

For the local `porec.smk` template, expected `config.yaml` keys are:

```yaml
fasta: /path/to/genome.fa
gene: /path/to/genes.bed
modbase: C
datatype: pore-c
threads: 16
binsizes: [10000, 50000, 100000]
```

Use `datatype: pore-c` for AporeC/Pore-C. Use another value such as
`nanopore` or `ont-wgs` only when contact matrices should not be required by
`rule all`.

## Skill Routing

Select only the stages supported by the input:

| Stage | Skill |
|---|---|
| Input triage and execution plan | `epigenomics/methylation/aporec-methylation-workflow` |
| POD5/FAST5 or unaligned modBAM to aligned sorted indexed modBAM | `epigenomics/methylation/dorado-modbase-prep` |
| modBAM to bedMethyl, QC, bigWig, CG/CHG/CHH annotation | `epigenomics/methylation/modkit-pileup` |
| AporeC/Pore-C BAM to pairs, concatemer QC, `.cool` matrices | `epigenomics/methylation/aporec-porec-contact` |
| DMR from modkit bedMethyl | `epigenomics/methylation/modkit-dmr` |
| Gene-level methylation plots from bigWig | `epigenomics/methylation/gene-plot` |
| TAD/compartment/loop analysis from `.cool`/`.mcool` | `3d-dna/3d-dna-analysis` and its subskills |

## Long-Read Methylation Rules

- Require `MM` and `ML` tags for modkit. A coordinate-sorted indexed BAM is
  not enough if those tags are absent.
- Preserve tags during alignment. Use Dorado alignment or another SAM/BAM
  route that keeps modified-base tags; do not convert through FASTQ.
- For plant/crop 5mC, prefer all-cytosine 5mC calls and split/annotate
  `CG`, `CHG`, and `CHH` downstream. CpG-only models cannot recover CHG/CHH.
- For `modbase: A`, interpret the target as 6mA/open chromatin only if the
  basecaller actually produced an adenine modification tag such as `A+a`.
- For `modbase: GC` in the local workflow, keep the special GC/CG motif logic
  separate from standard plant `C` context splitting.

## Second-Generation Boundary

Keep WGBS/EM-seq/RRBS separate:

- They start from short paired FASTQ and use bisulfite-aware aligners such as
  Bismark or bwa-meth, not Dorado.
- They infer methylation from C-to-T conversion and do not contain ONT
  per-read `MM`/`ML` probability tags.
- Their output cytosine reports can be converted to bedGraph/bigWig for
  plotting, but they should not be passed to `modkit pileup` or AporeC
  contact-matrix stages.
- Compare long-read and short-read methylation only after harmonizing genome
  build, chromosome naming, context definition, strand policy, coverage
  thresholds, and value scale.

## Planning Output

When this skill is used, produce a concise plan containing:

- Data classification: AporeC/Pore-C, ordinary ONT, PacBio, or second-gen.
- Whether methylation is already in modBAM tags or Dorado prep is required.
- Whether contact matrix stages are valid.
- Selected downstream skills and execution order.
- Expected primary outputs: bedMethyl, `.tbi`, bigWig, profile plots,
  `.pairs.gz`, high-order distribution plots, and `.cool` matrices.
- A blocked reason when raw signal, `MM`/`ML` tags, reference FASTA, or sample
  design is missing.

Never report that methylation was successfully called from an ordinary BAM
without checking modified-base tags.
