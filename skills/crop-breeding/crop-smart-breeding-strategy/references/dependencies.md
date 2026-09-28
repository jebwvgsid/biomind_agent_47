# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

依赖按正文真实命令和所调用脚本的 import/library 列表确认；未声明最低版本，不推断已安装。

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原方法输入

- data_inventory
- breeding_goal
- trait_metadata

## 原方法输出

- strategy_report
- selected_skills
- data_requirements
- validation_plan

## 适用条件

- User asks which phenotype prediction skill, ML model, or smart breeding route fits their data.
- User has multiple data types and needs an execution plan.

## 不适用条件

- User has already selected a specific skill and asks to execute it directly.

