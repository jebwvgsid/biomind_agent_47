# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

mixed

## 软件/库

- xgboost
- GENIE3
- GEOquery

## 方法能力（描述）

- xgboost-transcriptome-prediction
- regulatory-network

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原方法输入

- expression_matrix
- phenotype_table
- species_ortholog_table
- condition_metadata

## 原方法输出

- conserved_response_genes
- xgboost_prediction
- important_genes
- tf_network_candidates

## 适用条件

- User has expression matrices plus phenotype measurements and wants interpretable gene-to-phenotype prediction.
- User asks for conserved response genes, ortholog-guided feature reduction, XGBoost NUE prediction, or GENIE3 TF candidates.

## 不适用条件

- User has only genotype marker data.
- User wants differential expression analysis only, without phenotype prediction.

