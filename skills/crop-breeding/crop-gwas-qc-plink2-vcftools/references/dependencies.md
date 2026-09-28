# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

python

## 软件/库

- pandas
- numpy
- scipy
- matplotlib
- plink2
- bcftools
- vcftools
- beagle
- openjdk

## 方法能力（描述）

- crop-gwas-qc
- plink2
- vcf-qc
- beagle-imputation

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原资料声明的软件约束

- `python=3.11`（原依赖准备说明，未在本轮求解/安装验证）

## 原方法输入

- vcf
- pfile
- bfile
- phenotype_table
- qc_parameters

## 原方法输出

- qc_genotype_prefix
- sample_overlap
- pca
- ld_prune
- qc_report

