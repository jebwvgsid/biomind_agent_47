# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

r

## 软件/库

- Seurat
- Matrix
- yaml

## 方法能力（描述）

- scrna-ingest
- scrna-seurat-object

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原方法输入

- raw_count_matrix
- tenx_mtx
- tenx_h5
- seurat_rds
- sample_metadata

## 原方法输出

- raw_seurat_object
- sample_manifest

## 适用条件

- Workflow stage is data_ingest.
- User needs to build a Seurat object from 10x mtx, 10x h5, RDS, or RData input.

## 不适用条件

- Bulk RNA-seq

