# NCBI Entrez Fetch - 用户用法指南

## 这个 skill 是什么

这个 skill 用来在你已经知道 NCBI ID 或 accession 之后，把对应记录取回来。它解决的是“取内容”的问题，不解决“用关键词找记录”的问题。

适合输入 PMID、Gene ID、TaxID、核酸 accession、蛋白 accession、Assembly UID、SRA UID、BioSample UID、BioProject UID，然后让 agent 下载 FASTA、GenBank、PubMed 摘要、Gene summary、Taxonomy 记录或其他 NCBI 元数据。

如果你只有关键词，先让 agent 用 `ncbi-entrez-search`。如果你要从一个数据库跳到另一个数据库找关联 ID，先让 agent 用 `ncbi-entrez-link`。

## 前置条件

需要 Python 环境里有 Biopython：

```bash
pip install biopython
```

NCBI Entrez 请求需要邮箱。你可以在提示词里告诉 agent，也可以设置环境变量：

```bash
set NCBI_EMAIL=your.email@example.com
set NCBI_API_KEY=optional_api_key
```

## 快速开始

直接告诉 agent：你已有的 ID 是什么，以及你想要什么输出。

- “根据这些 nucleotide accession 下载 FASTA：NM_007294, NM_007295。”
- “根据 PMID 32228226 和 35412348 获取标题、期刊和摘要。”
- “下载 NM_007294 的 GenBank 记录。”
- “获取 NCBI Gene ID 672 的 summary。”
- “根据 TaxID 4547 和 4577 获取分类信息。”

## 标准提示词示例

### 下载核酸 FASTA

> “我有 nucleotide accession：NM_007294 和 NM_007295。请下载它们的 FASTA 序列，保存为 `brca_sequences.fasta`。”

### 下载 GenBank 注释记录

> “请获取 NM_007294 的完整 GenBank 记录，我要查看 CDS 和 feature 注释。”

### 下载蛋白 FASTA

> “请根据蛋白 accession NP_009225 下载 protein FASTA，保存为 `.faa` 文件。”

### 获取 PubMed 摘要

> “请根据 PMID 32228226 获取文章标题、期刊、年份、作者和摘要。”

### 获取 Gene Summary

> “请获取 NCBI Gene ID 672 的简洁信息，包括 gene symbol、description、organism 和 functional summary。”

### 获取 Taxonomy 信息

> “请根据 TaxID 4547 和 4577 获取 taxonomy 记录，并汇报 scientific name、rank 和 lineage。”

### 批量获取

> “请读取 `ids.txt` 里的 ID，批量下载 nucleotide FASTA，并把失败的 ID 单独报告出来。”

## Agent 会做什么

1. 判断你的 ID 类型和起始数据库。
2. 选择 `efetch()` 获取完整记录，或选择 `esummary()` 获取简洁元数据。
3. 选择合适输出格式，例如 FASTA、GenBank、GenPept、XML、TSV 或 JSONL。
4. 设置 Entrez email/API key。
5. ID 很多时分批请求，避免请求过大或触发限速。
6. 保存输出文件。
7. 检查输出是否为空、格式是否符合预期。
8. 汇报输出路径、成功数量、失败 ID 和注意事项。

## 常见任务路径

- nucleotide accession -> FASTA 序列
- nucleotide accession -> GenBank 注释记录
- protein accession -> FASTA 序列
- protein accession -> GenPept 注释记录
- PMID -> PubMed 标题、期刊、作者、摘要
- Gene ID -> NCBI Gene summary
- TaxID -> 分类名、rank、lineage
- Assembly UID -> 组装元数据
- SRA UID -> SRA 元数据，不是 FASTQ 原始数据

## 提示词建议

- 如果知道数据库，请明确写出：`nucleotide`、`protein`、`pubmed`、`gene`、`taxonomy` 等。
- 说明你要“完整记录”还是“简洁 summary”。
- 序列任务要说明 FASTA 还是 GenBank/GenPept。
- PubMed 任务要说明是否需要 abstract。
- ID 很多时，把 ID 放进文件，并要求 batch fetch。
- 这个 skill 负责取记录，不负责关键词搜索、GEO 数据集筛选、BLAST 或 FASTQ 下载。

## 边界

- 只有关键词时，先用 `ncbi-entrez-search`。
- 需要跨 NCBI 数据库找关联 ID 时，先用 `ncbi-entrez-link`。
- GEO 数据集筛选、GSE/GSM/GPL/GDS 解释和 GEO-to-SRA，更适合用 `ncbi-geo-data`。
- 序列相似性搜索应该交给 BLAST skill。
