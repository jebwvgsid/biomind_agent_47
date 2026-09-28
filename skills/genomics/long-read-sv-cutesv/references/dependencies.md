# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

bash

## 软件/库

- cutesv
- samtools
- bcftools

## 方法能力（描述）

- long-read-sv
- cutesv-calling

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原方法输入

- reference_fasta
- sorted_bam
- platform

## 原方法输出

- cutesv_vcf
- sv_summary

## 适用条件

- User explicitly requests cuteSV.
- User requests multi-caller validation or benchmarking for long-read SV.
- Sniffles2 is unavailable or failed and the user accepts an alternate caller.

## 不适用条件

- Vague long-read SV requests; use genomics/long-read-sv-sniffles2 first.
- Short-read WGS; use genomics/wgs-manta-sv.

