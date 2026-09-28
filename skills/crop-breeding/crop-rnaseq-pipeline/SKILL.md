---
name: crop-rnaseq-pipeline
description: Build crop bulk RNA-seq count and TPM matrices from FASTQ or BAM inputs using nf-core/rnaseq-style
  workflows. Use for crop transcriptome upstream QC, alignment, quantification, MultiQC, and handoff to crop-rnaseq-de;
  do not use when the user already has a count/TPM matrix or only wants DEG plotting.
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

# Crop RNA-seq Pipeline

Use this skill for crop bulk RNA-seq upstream processing:

```text
FASTQ/BAM -> QC -> alignment/quantification -> gene counts/TPM -> MultiQC -> crop-rnaseq-de handoff
```

## Trigger Conditions

- User asks for RNA-seq FASTQ to count matrix, crop transcriptome processing, expression matrix construction, STAR/Salmon, HISAT2, nf-core/rnaseq, or batch RNA-seq QC/quantification.
- Input is FASTQ/BAM plus a crop genome FASTA and GFF/GTF annotation.

Do not use for:

- Existing count matrix + metadata DEG analysis: use `crop-rnaseq-de`.
- Existing DE table visualization: use `crop-diff-visualizer`.
- No-replicate TPM fold-change screening: use `crop-rnaseq-de`.

## Inputs

Required:

- `samplesheet.csv` with at least `sample,fastq_1,fastq_2,tissue,genotype,condition,replicate,strandedness`
- `genome_fasta`
- `annotation_gff_or_gtf`
- `output_dir`

Optional:

- `aligner`: `star_salmon`, `star_rsem`, `hisat2`, or `salmon`
- `strandedness`: `auto`, `forward`, `reverse`, or `unstranded`
- `threads`, `genome_index`, `species_name`

## Crop-Specific Rules

- Never default to a human reference genome.
- Require user-provided `genome_fasta` and `annotation_gff_or_gtf` unless an explicit crop reference/index is provided.
- Preserve `gene_id`, `transcript_id`, and `gene_name` mapping when parsing outputs.
- Warn for polyploid or fragmented crop genomes; do not assume one gene ID maps to one biological locus.

## Workflow

1. Validate the samplesheet:
   `python "${SKILL_DIR}/scripts/build_samplesheet.py" --input samplesheet.csv --output validated.samplesheet.csv --check-files`
2. Dry-run the nf-core command:
   `bash "${SKILL_DIR}/scripts/run_nfcore_rnaseq.sh" --samplesheet validated.samplesheet.csv --genome-fasta genome.fa --annotation genome.gff3 --outdir results/rnaseq_pipeline --dry-run`
3. Run the workflow after reviewing the command.
4. Parse outputs:
   `python "${SKILL_DIR}/scripts/parse_nfcore_outputs.py" --nfcore-dir results/rnaseq_pipeline --outdir results/rnaseq_pipeline/parsed`
5. Use `handoff/crop_rnaseq_de.command.sh` for DEG analysis.

## Outputs

Every output directory should contain:

- `report.md`
- `result.json`
- `counts/gene_counts.raw.tsv`
- `counts/gene_counts.length_scaled.tsv`
- `counts/gene_tpm.tsv`
- `counts/transcript_tpm.tsv`
- `qc/multiqc_report.html`
- `qc/sample_mapping_rate.tsv`
- `handoff/crop_rnaseq_de.command.sh`
- `handoff/sample_metadata.template.tsv`
- `handoff/count_matrix_for_de.tsv`
- `reproducibility/commands.sh`, `params.yaml`, `checksums.sha256`

## Gotchas

- Chromosome names in FASTA, GFF/GTF, and BAM/quantification outputs must match.
- For autopolyploid crops, expression assigned to highly similar homeologs may be ambiguous.
- For no biological replicates, do not try to infer statistical DEG here; hand off TPM to `crop-rnaseq-de`.

## Tests

Validate samplesheet parsing and command generation with independent validation data or user-provided sample metadata; do not keep local example samplesheets inside this skill.
