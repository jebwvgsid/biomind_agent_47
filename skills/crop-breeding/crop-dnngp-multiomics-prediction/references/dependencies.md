# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

python

## 软件/库

- tensorflow
- numpy
- pandas
- scikit-learn

## 方法能力（描述）

- upstream-dnngp
- multiomics-prediction

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原方法输入

- genotype_matrix
- expression_matrix
- metabolite_matrix
- phenotype_table
- omics_metadata

## 原方法输出

- single_omics_performance
- multiomics_performance
- feature_importance
- predicted_trait

## 适用条件

- User asks for DNNGP, multi-omics genomic prediction, SNP-only vs omics comparison, or GT21 multi-omics prediction.
- At least one feature matrix and a phenotype table are available.

## 不适用条件

- User only has multi-environment phenotype and weather data without omics matrices.
- User needs pangenome structural-variant genomic selection.

