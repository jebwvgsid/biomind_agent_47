---
name: wgs-short-read-mapping
description: Map paired-end short-read WGS resequencing FASTQ to a reference genome with BWA-MEM or BWA-MEM2, samtools,
  and duplicate marking, producing coordinate-sorted indexed BAMs for downstream SNP/InDel, SV, and CNV calling.
  Use before WGS variant calling when users provide FASTQ rather than ready BAM files. Do not use for RNA-seq, long-read,
  or assembly comparison data.
metadata:
  version: 1.3.0
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

# WGS short-read mapping

Convert paired FASTQ to sorted, duplicate-marked BAMs; mark and keep duplicates
for calling unless the agreed method says otherwise. Read these method resources:

- [mapping-methods.md](references/mapping-methods.md): sample/read groups, bounded
  input checks, BWA/Picard commands, thresholds, parameters and expected products.
- [runtime-and-reference.md](references/runtime-and-reference.md): required index
  bundle layout, reference identity, Java/tool selection and preservation on failure.
- [failure-publication.md](references/failure-publication.md): native return codes,
  partial results and factual reporting.

Confirm read groups, reference assembly and duplicate policy from the task. Do not
infer assembly identity from matching contig names. Ploidy remains a downstream
scientific question; mapping does not resolve it. Keep original FASTQ/reference
bytes and existing results unchanged. Dependency preparation needs explicit
permission and is distinct from scientific execution or scientific success.

Use the actual CLI examples in the references from the analysis project. No
BioMind runtime object, framework step identity or private tool protocol is needed.
A directory existing or a zero exit status alone does not establish complete valid
scientific results. Assess the actual native outputs and documented checks.
