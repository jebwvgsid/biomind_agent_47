# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

bash

## 软件/库

- manta
- samtools
- bcftools

## 方法能力（描述）

- short-read-sv
- manta-config
- manta-workflow

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原方法输入

- reference_fasta
- sorted_bam
- sorted_cram
- bam_list

## 原方法输出

- manta_run_dir
- diploid_sv_vcf
- candidate_sv_vcf
- sv_summary

## 适用条件

- Illumina/MGI paired-end WGS BAM/CRAM structural variant calling.
- Short-read SV requests where no caller is specified.
- Germline or somatic Manta workflows when the sample design is clear.

## 不适用条件

- ONT/PacBio long reads; use genomics/long-read-sv-sniffles2.
- Read-depth CNV-only analysis; use genomics/wgs-cnvnator-cnv.
- RNA-seq variant calling unless the user explicitly requests Manta RNA mode.

