---
name: remote-blast
description: Run remote NCBI BLAST for one or a few public nucleotide/protein sequences against NCBI public databases.
  Use for quick unknown-sequence identification, public homolog searches, nr/nt/refseq screening, and small exploratory
  jobs; use local BLAST for private sequences, custom databases, reproducible batch searches, or large jobs.
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

# Remote NCBI BLAST Search

Use this skill for small, public-database sequence searches through NCBI BLAST. Do not submit private, unpublished, sensitive, or project-confidential sequences to remote BLAST.

## When To Use

- Identify an unknown public nucleotide or protein sequence.
- Check whether a candidate sequence has obvious homologs in NCBI `nr`, `nt`, RefSeq, or model organism databases.
- Run a one-off query where local database setup would be unnecessary.
- Obtain a quick taxonomic or annotation clue before deeper local analysis.

Do not use this skill for private sequences, custom genomes, many-query batch searches, strict reproducibility, raw FASTQ analysis, or final orthology/phylogeny claims.

## Program And Database Choice

| Query | Typical program | Typical database |
|---|---|---|
| nucleotide | `blastn` | `nt`, `refseq_rna`, `refseq_genomic` |
| protein | `blastp` | `nr`, `swissprot`, `refseq_protein` |
| nucleotide to protein | `blastx` | `nr`, `swissprot`, `refseq_protein` |
| protein to translated nucleotide | `tblastn` | `nt`, `refseq_genomic` |

Use organism filters only when they answer the question; overly narrow filters can hide useful homologs.

## Core Workflow

1. Confirm the query is safe to submit to NCBI public services.
2. Validate FASTA format and determine nucleotide/protein type.
3. Choose BLAST program, database, organism filter if needed, e-value, hit limit, and low-complexity filtering behavior.
4. Submit the remote BLAST job and record the RID, query time, database, and parameters.
5. Wait for completion without aggressive polling.
6. Parse hits into a compact table with accession, title, organism, identity, alignment length, coverage, e-value, and bitscore.
7. Fetch top hit records only if the user needs sequence or annotation details.

## Python Pattern

```python
from Bio.Blast import NCBIWWW, NCBIXML

query_fasta = """>query1
MEEPQSDPSVEPPLSQETFSDLWKLLPEN
"""

result_handle = NCBIWWW.qblast(
    program="blastp",
    database="nr",
    sequence=query_fasta,
    expect=1e-5,
    hitlist_size=20,
    format_type="XML",
)

blast_record = NCBIXML.read(result_handle)
for alignment in blast_record.alignments[:10]:
    hsp = alignment.hsps[0]
    print(alignment.accession, alignment.hit_def, hsp.expect, hsp.identities, hsp.align_length)
```

If command-line reproducibility is preferred, use NCBI BLAST+ remote mode when available:

```bash
blastp -remote \
  -query query.faa \
  -db nr \
  -evalue 1e-5 \
  -max_target_seqs 20 \
  -outfmt "6 qseqid sacc stitle pident length evalue bitscore qcovs" \
  -out remote_blast.tsv
```

## Output Guidance

When useful, produce:

- `remote_blast.xml`: raw XML output for reproducible parsing.
- `remote_blast.tsv`: tabular hit summary.
- `top_hits.tsv`: top filtered hits per query.

Report:

- query count and query type
- program, database, organism filter, e-value, hit limit, and format
- RID or equivalent job identifier when available
- top hits with accession, title, organism, identity, coverage, e-value, and bitscore
- no-hit queries and likely causes
- next step, such as `ncbi-entrez-fetch`, `local-blast`, `sequence-similarity`, or `ensembl-database`

## Practical Limits

- NCBI remote BLAST is shared public infrastructure; keep jobs small and polling conservative.
- Remote database contents can change. For reproducible analysis, download a database snapshot and use `local-blast`.
- Web/remote BLAST may mask low-complexity sequence depending on program defaults; report filtering choices when they matter.
- Top BLAST hit is not proof of orthology. Use reciprocal search, synteny, domain architecture, or curated orthology databases for stronger claims.
- For long-running or repeated jobs, prefer local BLAST or an explicit NCBI BLAST URL API/RID workflow.

## Related Skills

- `skills/sequence/search/local-blast` - Custom or large-scale BLAST.
- `skills/sequence/search/sequence-similarity` - Distant homologs, HMMER, and reciprocal best hits.
- `skills/databases/ncbi/entrez-fetch` - Fetch BLAST hit records.
- `skills/databases/ncbi/entrez-search` - Search NCBI databases before selecting a BLAST database or organism filter.
