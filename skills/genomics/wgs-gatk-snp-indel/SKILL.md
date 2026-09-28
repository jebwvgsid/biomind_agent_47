---
name: wgs-gatk-snp-indel
description: Call short-read WGS SNPs and InDels with the GATK HaplotypeCaller GVCF workflow, including per-sample
  GVCFs, GenomicsDBImport or CombineGVCFs, joint genotyping, hard filtering, validation, and summary counts. Use
  for high-confidence cohort resequencing variant calling from duplicate-marked BAMs. Do not use for RNA-seq, long-read
  SV, CNV, or assembly comparison.
metadata:
  version: 1.2.0
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

# WGS GATK SNP/InDel calling

Before selecting biological parameters or generating HaplotypeCaller, joint
calling, filtering and validation commands, read
[calling-methods.md](references/calling-methods.md). It contains the actual CLI
commands, input/output conventions and scientific parameters.

Confirm sample ploidy, reference assembly, read groups, filter applicability and
thresholds with the task. An unanswered biological question stays unresolved.
Use duplicate-marked coordinate-sorted BAMs and matching reference indexes.
This revision uses CombineGVCFs; GenomicsDB is a different explicitly selected
method. Preserve native return codes, stderr and previous files.

Use verified Python/GATK/Java executables where needed, with explicit PATH and
JAVA_HOME. A broken interpreter shebang does not prove GATK is absent.
Dependency installation or upgrades need their own explicit authorization and
are not scientific results. No framework step, private runtime object or model
loop is required to run the documented commands.
