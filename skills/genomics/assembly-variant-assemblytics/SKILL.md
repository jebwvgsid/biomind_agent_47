---
name: assembly-variant-assemblytics
description: Optional assembly-vs-reference structural variant summarization with Assemblytics from MUMmer delta
  alignments. Use as supplemental assembly-level SV analysis when the user explicitly requests Assemblytics or has
  a suitable delta file.
metadata:
  version: 1.0.0
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

# Optional Assembly SV with Assemblytics

Use Assemblytics only as supplemental assembly-level analysis or when the user explicitly requests it. The default assembly-level workflow is `genomics/assembly-variant-syri`.

## Execution Guidance

Input is a MUMmer delta file from a reference-vs-query assembly alignment. If no delta exists, generate it only when both assemblies are provided and the plan includes this optional stage.

Check the installed command spelling before use because packages may expose `Assemblytics` or `assemblytics`.

```bash
nucmer --maxmatch -p "${OUTDIR}/03.variant_calling/out" \
  "${REFERENCE_ASSEMBLY}" "${QUERY_ASSEMBLY}"
delta-filter -m "${OUTDIR}/03.variant_calling/out.delta" \
  > "${OUTDIR}/03.variant_calling/out.filtered.delta"
Assemblytics "${OUTDIR}/03.variant_calling/out.filtered.delta" \
  "${OUTDIR}/04.filter/assemblytics" \
  "${UNIQUE_ANCHOR_LENGTH:-10000}" "${MIN_VARIANT_SIZE:-50}" "${MAX_VARIANT_SIZE:-10000000}"
```

## Outputs and Validation

Validate non-empty Assemblytics result tables/BED files and summarize SV classes. If the command produces no variants, distinguish valid no-call output from failed/empty files.
