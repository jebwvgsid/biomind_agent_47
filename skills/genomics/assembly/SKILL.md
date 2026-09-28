---
name: assembly
description: 用于 PacBio HiFi 数据的 hifiasm 组装流程，优先处理输入检查、软件检查、组装命令生成、结果文件判定与后续 GFA/FA 转换。
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

# Hifiasm Assembly

## 适用场景
当用户的任务涉及以下内容时，应优先使用本 skill：

- 使用 PacBio HiFi reads 进行基因组组装
- 使用 `hifiasm` 进行单倍型组装或主组装
- 检查 `.fastq.gz` / `.fq.gz` / `.fa.gz` / `.fasta.gz` 是否可作为 HiFi 输入
- 生成 `hifiasm` 运行命令
- 解释 `hifiasm` 的主要输出文件
- 将 `.gfa` 转为 `.fa`

## 目标
帮助智能体以**最小、安全、可验证**的方式完成以下步骤：

1. 检查输入文件是否存在、格式是否合理
2. 检查 `hifiasm` 是否已安装
3. 生成单步 `hifiasm` 组装命令
4. 检查输出文件是否生成
5. 在需要时，将 `.gfa` 转换为 FASTA
6. 不跨步执行，不在一步里做过多事情

---

## 重要原则

### 1. 一次只做一步
- 不要在一次命令执行 中同时做“组装 + 转 fasta + 统计 + 清理”
- 先跑组装，再看输出，再决定下一步

### 2. 优先只读检查
在真正执行组装前，优先检查：
- 输入文件路径
- 文件名
- 软件是否存在
- 当前目录空间是否合理
- 输出目录是否存在

### 3. 默认不删除文件
- 不要自动 `rm`
- 不要自动覆盖已有结果
- 如果输出前缀已存在，先提示用户或改用新前缀

### 4. 优先使用当前工作目录
若用户未指定输出目录：
- 默认在当前工作目录下创建结果目录
- 例如：`./hifiasm_run`

### 5. 如果缺少输入信息，先问用户
必须明确的关键信息包括：
- HiFi reads 文件路径
- 输出前缀
- 预期线程数（如果用户在意资源）
- 是否想要 primary assembly / haplotype-resolved assembly

---

## 输入识别规则

以下文件可以优先视为 HiFi 原始输入候选：

- `*.fastq`
- `*.fq`
- `*.fastq.gz`
- `*.fq.gz`
- `*.fa`
- `*.fasta`
- `*.fa.gz`
- `*.fasta.gz`

如果目录中有多个候选文件：
- 先列出来
- 不要擅自拼接多个文件，除非用户明确说明
- 如果用户说是一个样本拆分的多个 lane，可以建议后续 `cat` 合并，但不要默认执行

---

## 软件检查优先模板

如果需要先确认 `hifiasm` 是否可用，优先输出：

```bash
#!/bin/bash
echo "[PWD]"
pwd
echo
echo "[HIFIASM CHECK]"
which hifiasm || true
echo
hifiasm --help >/dev/null 2>&1
echo "[RETURNCODE] $?"
```
### 判定逻辑
- 如果 `which hifiasm` 有输出，且 help 返回码为 0，说明工具基本可用
- 如果不可用，优先使用 `先确认所需依赖（hifiasm）与目标环境，再请求用户明确安装授权`

---

## 输入检查推荐模板

如果用户让你先检查 HiFi 输入文件，优先输出：

```bash
#!/bin/bash
echo "[PWD]"
pwd
echo
echo "[CANDIDATE HIFI FILES]"
find . -maxdepth 3 \( -name "*.fastq" -o -name "*.fq" -o -name "*.fastq.gz" -o -name "*.fq.gz" -o -name "*.fa" -o -name "*.fasta" -o -name "*.fa.gz" -o -name "*.fasta.gz" \) | sort | head -n 50
```
---

## 单样本 hifiasm 组装推荐模板

### 标准主组装
当用户已经给了：
- HiFi 输入文件
- 输出前缀
- 线程数

优先输出类似下面命令：

```bash
#!/bin/bash
mkdir -p ./hifiasm_run
cd ./hifiasm_run

hifiasm -o sample.asm -t 32 /path/to/sample.hifi.fastq.gz
```
### 说明
- `-o sample.asm` 是输出前缀，不是最终单个文件名
- `-t 32` 是线程数，可根据用户环境调整
- 不要在同一步里自动加后处理

---

## 常见输出解释

`hifiasm` 常见输出包括：

- `sample.asm.p_ctg.gfa`
  - primary contig 图文件
- `sample.asm.a_ctg.gfa`
  - alternate contig 图文件
- 某些情况下还会有 haplotype 相关输出

如果用户问“组装结果在哪”：
优先告诉他先看：
- `*.p_ctg.gfa`
- `*.a_ctg.gfa`

---

## GFA 转 FASTA 推荐模板

如果 primary 组装已经完成，并且存在 `.p_ctg.gfa`，再单独执行这一步：

```bash
#!/bin/bash
cd ./hifiasm_run

awk '/^S/{print ">"$2; print $3}' sample.asm.p_ctg.gfa > sample.p_ctg.fa
awk '/^S/{print ">"$2; print $3}' sample.asm.a_ctg.gfa > sample.a_ctg.fa
```
### 注意
- 在执行这一步前，先确认 `.gfa` 文件存在
- 不要默认假设 `a_ctg.gfa` 一定存在
- 若只存在 `p_ctg.gfa`，只转换 primary

---

## 输出检查推荐模板

组装完成后，优先做最小检查：

```bash
#!/bin/bash
cd ./hifiasm_run

echo "[FILES]"
ls -lh

echo
echo "[PRIMARY GFA]"
ls -lh *.p_ctg.gfa 2>/dev/null || true

echo
echo "[ALT GFA]"
ls -lh *.a_ctg.gfa 2>/dev/null || true
```
---

## 智能体行为约束

### 应该做的
- 按当前用户请求选择所需操作，使用当前宿主命令工具执行示例
- 尽量使用只读检查作为第一步
- 如果 `hifiasm` 缺失，先提出具体安装方案并等待用户明确授权
- 如果输入路径不明确，先问用户
- 如果文件不存在，明确指出具体缺了什么
- 组装后先检查输出，再做转换或统计

### 不应该做的
- 不要一步执行完整流水线
- 不要自动清理中间文件
- 不要自动覆盖旧结果
- 不要擅自修改线程数到极大值
- 不要假设所有输入都是单个文件
- 不要把 `.gfa` 和 `.fa` 混为一谈

---

## 参数建议

### 线程数
若用户未指定线程数，可建议：
- 小测试：8
- 常规：16 或 32
- 但不要擅自按最大核数运行

### 输出前缀
若用户未指定，建议：
- `sample.asm`
- 或根据输入文件 basename 自动推断
但在执行前最好显式展示给用户

---

## 失败诊断提示

如果运行失败，优先考虑以下原因：
- `hifiasm` 未安装
- 输入文件路径错误
- 输入不是 HiFi reads
- 输出目录无写权限
- 磁盘空间不足
- 线程数过高导致资源竞争

---

## 成功判定
组装结果的科学检查条件：

### 软件检查步
- `which hifiasm` 找得到
- `hifiasm --help` 返回正常

### 输入检查步
- 能列出明确的候选 HiFi 文件

### 组装步
- 命令返回码为 0
- 输出目录中出现 `*.p_ctg.gfa` 或其他合理的 hifiasm 结果文件

### 转 fasta 步
- 生成 `.fa` 文件且非空

---

## 生成风格偏好
如果你决定使用本 skill：
- 优先生成 Bash
- 命令尽量直接、短、小步
- 输出中尽量包含 `[PWD]`、`[FILES]` 这类清晰标记，便于后续验证