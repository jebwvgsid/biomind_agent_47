# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

shell

## 软件/库

- bcftools
- vcftools
- htslib
- samtools
- bedtools
- plink
- plink2

## 方法能力（描述）

- pangenome-sv-selection
- variant-qc

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原方法输入

- snp_vcf
- sv_vcf
- phenotype_table
- gene_presence_absence
- gff3

## 原方法输出

- snp_heritability
- sv_heritability
- snp_vs_sv_prediction
- polygenic_score
- selection_index

## 适用条件

- User has SNP plus SV, indel, graph-pangenome, or gene presence-absence data for breeding prediction.
- User asks for pangenome-assisted genomic selection, SV-based heritability, LDpred2, or multi-trait selection.

## 不适用条件

- User only has bulk RNA-seq or transcriptome expression data.
- User needs GWAS-weighted deep learning rather than SV/pangenome feature evaluation.

