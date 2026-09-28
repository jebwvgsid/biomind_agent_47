# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

python

## 软件/库

- pytorch
- numpy
- pandas

## 方法能力（描述）

- upstream-cropformer
- attention-genomic-prediction

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原方法输入

- genotype_matrix
- phenotype_table
- marker_annotation

## 原方法输出

- predicted_trait
- important_snp
- attention_weights
- haplotype_candidates

## 适用条件

- User asks for Cropformer, CNN-attention genomic prediction, attention-based SNP mining, or unweighted Transformer phenotype prediction.
- Genotype and phenotype are available but GWAS weights are missing or should not be used.

## 不适用条件

- User has GWAS p-value weights and wants the weighted Transformer route specifically.
- User has only expression/omics matrices without genotype markers.

