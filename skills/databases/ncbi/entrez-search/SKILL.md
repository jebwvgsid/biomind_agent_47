---
name: entrez-search
description: Search NCBI Entrez databases such as PubMed, Gene, Nucleotide, Protein, SRA, GEO, BioProject, Assembly,
  and Taxonomy. Use when you need record IDs, result counts, searchable fields, or query translation before fetching
  or linking records; do not use when you already have final IDs and need full records.
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

# NCBI Entrez Search

Use this skill for the discovery step in NCBI databases. It should produce record IDs, counts, query diagnostics, and database field information. It should not download full FASTA/GenBank/PubMed records; use `ncbi-entrez-fetch` after IDs are selected.

## When To Use

- Search PubMed papers by keyword, author, journal, date, MeSH-like terms, or title words.
- Search NCBI Gene, Nucleotide, Protein, SRA, BioProject, Assembly, GEO/GDS, or Taxonomy.
- Count how many records match a query before deciding whether to download.
- Inspect searchable fields and database links with `einfo`.
- Run a broad cross-database count with `egquery`.
- Build reproducible query strings before passing IDs to `entrez-fetch` or `entrez-link`.

Do not use this skill for full record download, GEO-specific metadata parsing, BLAST searches, or Ensembl/UniProt annotations.

## Core Workflow

1. Choose the NCBI database: usually `pubmed`, `gene`, `nucleotide`, `protein`, `sra`, `gds`, `bioproject`, `assembly`, or `taxonomy`.
2. Build an explicit query with field tags when possible.
3. Run `Entrez.esearch()` to get count and IDs.
4. If the field syntax is unclear, run `Entrez.einfo(db=...)`.
5. If the database is unclear, run `Entrez.egquery(term=...)`.
6. Report the database, query string, total count, returned IDs, and whether the query was translated.

## Query Patterns

```python
from Bio import Entrez

Entrez.email = "your.email@example.com"
term = "Saccharum[orgn] AND sucrose synthase[title]"
handle = Entrez.esearch(db="pubmed", term=term, retmax=20)
record = Entrez.read(handle)
handle.close()
print(record["Count"], record["IdList"])
```

Useful databases:

| Database | Use |
|---|---|
| `pubmed` | papers and biomedical literature |
| `gene` | NCBI Gene records |
| `nucleotide` | DNA/RNA sequences |
| `protein` | protein sequences |
| `sra` | sequencing runs/experiments |
| `gds` | GEO DataSets and Series |
| `bioproject` | project-level accessions |
| `assembly` | genome assemblies |
| `taxonomy` | organism taxonomy IDs |

Useful field tags:

| Field | Example |
|---|---|
| organism | `Saccharum[orgn]` |
| gene symbol | `BRCA1[gene]`, `SPS[sym]` |
| accession | `NM_001301717[accn]` |
| title | `drought[title]` |
| author | `Smith J[auth]` |
| journal | `Nature[jour]` |
| publication date | `2024[pdat]` |
| sequence length | `1000:5000[slen]` |
| BioProject | `PRJNA123456[bioproject]` |

## Common Tasks

Search one NCBI database:

```bash
python "${SKILL_DIR}/examples/search_entrez.py" --db pubmed --term "Saccharum drought RNA-seq" --max-results 10
```

Inspect database fields and links:

```bash
python "${SKILL_DIR}/examples/database_info.py" --db sra
```

Count a term across all NCBI databases:

```bash
python "${SKILL_DIR}/examples/global_query.py" --term "Saccharum"
```

## Report Back

For each search, report:

- database searched
- exact query string
- total NCBI count
- number of IDs returned
- returned IDs or accessions
- query translation when NCBI rewrites the query
- recommended next skill, usually `ncbi-entrez-fetch`, `ncbi-entrez-link`, or `ncbi-geo-data`

## Related Skills

- `skills/databases/ncbi/entrez-fetch` - fetch records after IDs are selected.
- `skills/databases/ncbi/entrez-link` - link IDs across NCBI databases.
- `skills/databases/ncbi/geo-data` - GEO-specific dataset discovery and GEO-to-SRA workflows.
- `citation-management method (not included in this package)` - turn PubMed results into citations/BibTeX.

## Extended References

- `references/usage-guide.md` - English guide.
- `references/usage-guide.zh.md` - 中文用法说明。
- `examples/search_entrez.py` - Search one database.
- `examples/database_info.py` - Inspect database fields and links.
- `examples/global_query.py` - Count a term across all NCBI databases.
