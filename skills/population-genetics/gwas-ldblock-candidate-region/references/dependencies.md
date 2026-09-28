# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

bash

## 软件/库

- bedtools
- LDBlockShow
- ShowLDSVG
- python

## 方法能力（描述）

- candidate-gene-window
- ldblock
- local-manhattan

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原方法输入

- gwas_table
- significant_sites
- vcf
- gff3
- pvalue_threshold
- window_size

## 原方法输出

- significant_windows
- candidate_genes
- in_gene_sites
- ldblock_script
- ldblock_figures

