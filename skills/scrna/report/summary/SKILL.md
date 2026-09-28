---
name: summary
description: 汇总 scRNA 分析全过程的 QC、预处理、聚类、注释、marker、DE、富集和轨迹结果，生成报告、stage_status.tsv 和 reproduce.sh。Use at the end
  of a scRNA workflow; do not use as a substitute for missing analysis steps.
compatibility: 需要本技能 references/dependencies.md 列明的现有软件；不自动安装。输入输出位于分析项目。
---


## 资源定位与依赖

使用当前 Agent 的标准技能发现与文件读取能力加载本文件。`scripts/`、`references/`、
`assets/` 相对本 SKILL.md 的实际目录；输入、输出和样本清单相对本次分析项目。
命令中的 `${SKILL_DIR}` 必须先设为实际技能目录的绝对路径；该变量不由宿主自动提供。
例如，从已加载位置解析目录后，以带引号的脚本绝对路径执行，保持工作目录为分析项目，
不要在技能安装目录写结果。跨技能资源按其实际目录定位；分享完整技能包时保留目录结构。
软件和参考数据库来自已有环境或用户显式提供的位置。依赖见
[dependencies.md](references/dependencies.md)；安装、升级或修改环境需要事先明确授权。
本文命令可由任意提供命令执行能力的 Agent 或终端使用；不会在读取技能时安装或体检环境。

# scRNA Analysis Summary Report

Use this skill to create a final, auditable summary after scRNA analysis.

## Required Inputs

- Output directory containing one or more standard BioMind scRNA outputs
- The commands or scripts used for reproduction
- Stage completion evidence

## Workflow

1. Verify expected artifacts for the selected workflow stages.
2. Summarize cell counts, filtering, clustering, annotation, marker, DE,
   enrichment, and trajectory outputs that actually exist.
3. Record missing optional outputs as `not_run` or `not_requested`, not failed.
4. Write a concise scientific report with methods, parameters, and limitations.
5. Write `stage_status.tsv` and `reproduce.sh`.

## Outputs

- `{output_dir}/report.md`
- `{output_dir}/stage_status.tsv`
- `{output_dir}/reproduce.sh`

## Rules

- Do not mark the workflow complete if the latest executed command only wrote a
  script and did not run the promised analysis.
- Do not invent results for optional stages that were not requested.
- If an upstream required artifact is missing, ask the user or report the exact
  missing artifact instead of looping through read-only checks.

