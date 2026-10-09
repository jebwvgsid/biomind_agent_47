# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

## 语言/解释器

python

包装脚本支持 Python 3.8 及以上；实际 Mustache 依赖以所选现有环境为准。

## 软件/库

- mustache

已有 ay-lab 源码可通过包装脚本的 `--mustache-script` 使用，差异脚本可通过
`--diff-script` 指定；无需为获得 `python -m mustache` 入口而重新安装。
本机路径从 runtime 登记或用户指定位置取得。登记命中不等于已通过运行检查。

## 方法能力（描述）

- loop-calling
- differential-loops
- chromatin-loops

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原方法输入

- cool_matrix
- mcool_matrix
- resolution

## 原方法输出

- loop_calls
- loop_summary
- differential_loop_calls

## 适用条件

- Workflow stage is loop_calling.
- User asks to call, detect, or identify chromatin loops from contact matrices.
- User asks for Mustache loop outputs or differential loops.

## 不适用条件

- Loop anchor classification after loop calls already exist.
- A/B compartment calling or compartment transition analysis.
- TAD calling, TAD boundary comparison, or differential TAD testing.
