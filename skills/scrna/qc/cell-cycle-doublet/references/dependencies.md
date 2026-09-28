# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

r

## 软件/库

- Seurat
- yaml
- ggplot2
- dplyr

## 方法能力（描述）

- scrna-cell-cycle
- scrna-doublet-qc

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原方法输入

- qc_filtered_seurat_object
- species
- cell_cycle_gene_sets

## 原方法输出

- qc_annotated_seurat_object
- cell_cycle_table
- doublet_table

## 适用条件

- User asks for cell-cycle scoring, regression decision, doublet detection, or enhanced scRNA QC.
- Workflow stage is cell_cycle_doublet_qc.

## 不适用条件

- The user only requested basic mitochondrial/count filtering.
- Required species-specific gene sets or doublet packages are unavailable.

