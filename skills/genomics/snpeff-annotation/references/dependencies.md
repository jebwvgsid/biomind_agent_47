# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

bash

## 软件/库

- snpeff
- openjdk
- gatk4
- samtools
- bcftools

## 命令

- snpEff
- java

## 方法能力（描述）

- snpeff-annotation
- snpeff-custom-db
- ann-effect-summary

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原方法输入

- variant_vcf
- reference_fasta
- annotation_gtf
- annotation_gff3
- annotation_genbank

## 原方法输出

- {'output_id': 'annotated_vcf', 'default_filename': 'annotated.vcf', 'format': 'vcf'}
- {'output_id': 'ann_summary', 'default_filename': 'ann_summary.tsv', 'format': 'tsv'}
- {'output_id': 'report', 'default_filename': 'report.md', 'format': 'markdown'}
- {'output_id': 'reproduce_script', 'default_filename': 'reproduce.sh', 'format': 'bash'}
- {'output_id': 'stage_status', 'default_filename': 'stage_status.tsv', 'format': 'tsv'}

## 适用条件

- Annotate an existing VCF using a matching database or custom reference and gene annotation.

## 不适用条件

- Raw reads still need variant calling.

