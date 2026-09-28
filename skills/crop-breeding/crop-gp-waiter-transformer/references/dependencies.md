# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

python

## 软件/库

- pytorch
- numpy
- pandas
- scikit-learn

## 方法能力（描述）

- upstream-gp-waiter
- gwas-weighted-prediction

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原资料声明的软件约束

- `python=3.9`（原依赖准备说明，未在本轮求解/安装验证）
- `pytorch==1.13.0`（原依赖准备说明，未在本轮求解/安装验证）
- `pytorch-cuda=11.7`（原依赖准备说明，未在本轮求解/安装验证）

## 原方法输入

- genotype_matrix
- phenotype_table
- gwas_summary
- marker_annotation

## 原方法输出

- prediction_table
- model_performance
- important_snps
- attention_weights
- snp_to_gene

## 适用条件

- User asks for GP-WAITER, GWAS-weighted Transformer, weighted SNP embedding, or interpretable crop phenotype prediction.
- A GWAS summary table can be aligned to the genotype marker matrix.

## 不适用条件

- No phenotype table is available for training or validation.
- Only transcriptome features are available without genotype markers.

