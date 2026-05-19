# NCBI GEO Data 中文用法说明

这个 skill 用来查 NCBI GEO 里的公开表达数据集。它的定位是“找数据、看元数据、找下载入口、关联 SRA”，不是直接做 RNA-seq 分析。

## 什么时候用

- 想找某个物种、组织、处理条件下的公开 RNA-seq / microarray / single-cell / ChIP-seq 数据。
- 想查看 `GSE`、`GSM`、`GPL`、`GDS` 的基本信息。
- 已有论文或 PMID，想知道有没有关联 GEO 数据。
- 已有 GSE 编号，想找对应 SRA 原始测序数据。
- 想下载 processed matrix、series matrix 或 supplementary count files。

## 什么时候不用

- 已经有 `SRR/ERR/DRR` 编号，下一步是下载 FASTQ。
- 要做比对、定量、差异表达、富集分析。
- 要查基因坐标、蛋白功能、同源基因。

## GEO 编号含义

| 编号 | 含义 | 用途 |
|---|---|---|
| `GSE` | Series | 一个完整研究或实验 |
| `GSM` | Sample | 单个样本 |
| `GPL` | Platform | 芯片或测序平台 |
| `GDS` | Curated DataSet | NCBI 整理过的数据集 |

## 常用查询方式

GEO 在 Entrez 里主要通过 `gds` 数据库查询。

```python
from Bio import Entrez

Entrez.email = "your.email@example.com"
term = "Saccharum AND drought AND RNA-seq AND gse[entry_type]"
handle = Entrez.esearch(db="gds", term=term, retmax=20)
record = Entrez.read(handle)
handle.close()
print(record["Count"], record["IdList"])
```

常用过滤条件：

| 目的 | 写法 |
|---|---|
| 限定物种 | `Saccharum[orgn]` |
| 标题关键词 | `drought[title]` |
| 描述关键词 | `stress[description]` |
| 只查 GSE | `gse[entry_type]` |
| 只查 curated GDS | `gds[entry_type]` |
| 高通量表达数据 | `expression profiling by high throughput sequencing[gdstype]` |
| 按 PubMed ID 查 | `35412348[pubmed]` |
| 按年份查 | `2024[pdat]` |

## 常见任务

查候选 GEO 数据集：

```bash
python examples/search_geo.py --term "Saccharum drought RNA-seq" --max-results 10
```

从 GSE 找对应 SRA runs：

```bash
python examples/geo_to_sra.py --gse GSE147507 --out GSE147507_sra_runs.txt
```

从 PMID 找关联 GEO 数据：

```bash
python examples/geo_from_pubmed.py --pmid 32228226
```

## 汇报时应该给用户什么

每个候选数据集至少给：

- GEO accession
- 标题
- 物种
- 样本数
- 平台
- 数据类型
- 关联论文 PMID
- FTP/supplementary 下载入口
- 如果需要原始数据，列出 SRA runs

## 相关 skills

- `ncbi-entrez-search`：通用 NCBI 搜索。
- `ncbi-entrez-link`：GEO、SRA、PubMed 之间互相跳转。
- `ncbi-entrez-fetch`：获取记录摘要或完整 metadata。
- `gene-id-mapping`：下载表达矩阵后做 ID 转换。
