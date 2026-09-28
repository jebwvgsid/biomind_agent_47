# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

R

## 软件/库

- data.table
- tidyverse
- lme4
- mgcv
- caret

## 方法能力（描述）

- ceris-jgra
- gxe-reaction-norm

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原方法输入

- phenotype_multi_environment
- genotype_matrix
- environment_timeseries
- growth_stage_window

## 原方法输出

- best_environmental_index
- reaction_norm_parameters
- environment_specific_prediction
- stable_genotypes

## 适用条件

- User has phenotype observations across environments, locations, years, or management conditions.
- User asks for environmental index, reaction norm, GxE genomic selection, CERIS, or prediction in new environments.

## 不适用条件

- Phenotype data come from only one environment.
- No environmental profile or growth-window metadata can be supplied or estimated.

