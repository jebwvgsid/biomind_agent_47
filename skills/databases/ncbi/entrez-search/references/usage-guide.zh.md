# NCBI Entrez Search 中文用法说明

这个 skill 用来在 NCBI Entrez 数据库里“找记录”。它的输出通常是 ID、数量、查询字段和查询解释，不负责下载完整记录。

## 主要功能

| 函数 | 用途 |
|---|---|
| `esearch` | 在一个数据库里搜索，返回 ID 和 count |
| `einfo` | 查看数据库有哪些字段、链接和基本信息 |
| `egquery` | 在多个 NCBI 数据库里统计一个关键词有多少结果 |

## 什么时候用

- 查 PubMed 文献。
- 查 NCBI Gene、Nucleotide、Protein、SRA、GEO、BioProject、Assembly。
- 想知道一个关键词在某个数据库里有多少结果。
- 不知道某个数据库能用哪些字段过滤。
- 想先拿到 ID，再交给 `entrez-fetch` 下载完整记录。

## 什么时候不用

- 已经有 ID，要下载 FASTA、GenBank、XML 或 PubMed metadata。
- 要做 GEO 数据集详细解析。
- 要查 Ensembl 坐标、UniProt 蛋白功能或做 BLAST。

## 常见查询

| 目的 | 查询例子 |
|---|---|
| 查物种 | `Saccharum[orgn]` |
| 查标题 | `drought[title]` |
| 查基因 | `BRCA1[gene]`, `SPS[sym]` |
| 查 accession | `NM_001301717[accn]` |
| 查年份 | `2024[pdat]` |
| 查作者 | `Smith J[auth]` |
| 查序列长度 | `1000:5000[slen]` |

## 常用命令

查一个数据库：

```bash
python examples/search_entrez.py --db pubmed --term "Saccharum drought RNA-seq" --max-results 10
```

查看数据库字段：

```bash
python examples/database_info.py --db sra
```

跨数据库统计关键词：

```bash
python examples/global_query.py --term "Saccharum"
```

## 汇报格式

给用户返回：

- 搜索的数据库
- 精确 query
- 总记录数
- 返回的 ID 数量和 ID 列表
- NCBI query translation
- 下一步建议：`entrez-fetch`、`entrez-link` 或 `geo-data`
