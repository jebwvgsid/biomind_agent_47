# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

python

## 软件/库

- cooler
- pandas
- numpy
- scipy
- h5py

## 可选库

- CALDER
- data.table

## 方法能力（描述）

- calder2
- calder
- hierarchical-subcompartment
- sub-compartment-summary
- cooler-to-calder-dump

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原资料声明的软件约束

- `python=3.10`（原依赖准备说明，未在本轮求解/安装验证）

## 原方法输入

- cool_matrix
- mcool_matrix
- resolution
- feature_track

## 原方法输出

- sub_compartment_tracks
- compartment_summary

## 适用条件

- User explicitly asks for CALDER2 or sub-compartment labels.
- User needs hierarchical A/B sub-compartments such as A.1, A.2, B.1, B.2.

## 不适用条件

- Ordinary A/B compartment calling where PC1/eigenvector output is enough.
- Differential comparison from already existing compartment files.

