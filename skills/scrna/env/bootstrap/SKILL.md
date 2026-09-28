---
name: bootstrap
description: 准备和验证单细胞转录组 Seurat 基础运行环境。用于 Seurat 对象构建、10x 数据读取、单细胞 QC 和基础可视化前的环境检查或首次安装。Use when the task is to
  provision or verify a reusable R environment for scRNA-seq workflows.
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

# scRNA Environment Bootstrap

Use this skill to make sure the reusable Seurat runtime exists before any
single-cell analysis begins.

## Rules

Use an existing R environment with the packages in references/dependencies.md.
An existing successful registration can be reused without repeating checks.
Run this verification script when environment setup or a changed dependency is
actually the user's task. A failed check reports the missing package; it does
not authorize installation. Present the target environment and exact changes
and obtain explicit user permission before installing. Do not create sample-specific
environments or run scientific analysis from this dependency-check method.

## Quick Start

```bash
Rscript "${SKILL_DIR}/scripts/check_scrna_env.R"
```

## Expected Output

- Package load status for core scRNA packages
- Package versions for reproducibility
- Non-zero exit if any required package fails to load

## Notes

- This skill is environment-focused. It does not ingest data or run QC itself.
- Pair it with `scrna/ingest/data-download` and `scrna/qc/filter` for the first
  course workflow.
