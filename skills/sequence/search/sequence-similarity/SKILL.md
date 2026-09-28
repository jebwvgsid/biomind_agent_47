---
name: sequence-similarity
description: Find homologs, ortholog candidates, paralogs, distant protein family members, conserved domains, and
  reciprocal best hits with BLAST, PSI-BLAST, HMMER/MMseqs-style, profile, and RBH workflows. Use when simple one-way
  BLAST is insufficient for functional inference, cross-species comparison, or plant gene-family screening; do not
  use as the sole evidence for final phylogenetic inference.
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

# Advanced Sequence Similarity

Use this skill when sequence similarity is part of comparative genomics, functional inference, gene-family analysis, or ortholog candidate selection. It should connect search results to biological interpretation while keeping uncertainty explicit.

## When To Use

- Find homologs across species when a single BLAST hit is not enough.
- Screen plant gene families with many paralogs or duplicated loci.
- Identify distant protein family members using profiles, domains, or iterative searches.
- Generate ortholog candidates with reciprocal best hits before downstream validation.
- Compare domain architecture, coverage, taxonomic distribution, and hit consistency.

Do not use this skill for final species-tree or gene-tree inference, variant effect prediction, or local alignment visualization alone.

## Method Choice

| Need | Preferred method |
|---|---|
| Close homolog search in fixed FASTA databases | BLAST with coverage and identity filters |
| Large all-vs-all protein clustering | MMseqs2 or DIAMOND if available |
| Distant protein homologs | PSI-BLAST or HMMER profile search |
| Known domain/family detection | HMMER against Pfam or custom HMMs |
| Ortholog candidates between two species | reciprocal best hit plus coverage and synteny/context checks |
| Gene family expansion screening | all-vs-all search plus clustering and copy-number summary |

Use public orthology databases or Ensembl comparative genomics when available, but keep local sequence evidence for project-specific genomes.

## Core Workflow

1. Normalize query and target FASTA headers; keep a mapping table to original gene IDs.
2. Remove exact duplicates or isoforms only when that matches the biological question; otherwise retain transcript/protein isoform labels.
3. Choose the method based on expected evolutionary distance and database size.
4. Run searches with explicit e-value, coverage, identity, and hit-count thresholds.
5. Summarize hits by query with identity, coverage, e-value, bitscore, taxon/species, annotation, and domain context.
6. Separate homologs, ortholog candidates, paralogs, isoforms, and ambiguous multi-hit cases.
7. For high-confidence function transfer, require coverage/domain consistency and avoid relying on a short local match.

## Common Workflows

Reciprocal best hit:

```bash
blastp -query speciesA.faa -db speciesB_db -out A_vs_B.tsv \
  -evalue 1e-5 -max_target_seqs 10 \
  -outfmt "6 qseqid sseqid pident length evalue bitscore qlen slen qcovs"

blastp -query speciesB.faa -db speciesA_db -out B_vs_A.tsv \
  -evalue 1e-5 -max_target_seqs 10 \
  -outfmt "6 qseqid sseqid pident length evalue bitscore qlen slen qcovs"
```

Then identify pairs where A's best hit in B returns A as B's best hit, after applying minimum query and subject coverage.

PSI-BLAST for distant protein homologs:

```bash
psiblast -query query.faa -db nr_or_local_proteins \
  -num_iterations 3 -evalue 1e-5 -inclusion_ethresh 1e-3 \
  -out_ascii_pssm query.pssm -out psiblast_hits.txt
```

HMMER-style family search:

```bash
hmmbuild family.hmm aligned_family.sto
hmmsearch --tblout hmm_hits.tbl --domtblout hmm_domains.tbl family.hmm target_proteins.faa
```

MMseqs2-style large search when available:

```bash
mmseqs easy-search query.faa target.faa mmseqs_hits.m8 tmp \
  --format-output query,target,pident,alnlen,evalue,bits,qcov,tcov
```

## Evidence Rules

- Homology means shared ancestry; it does not guarantee identical function.
- Orthology should not be inferred from one-way best hit alone in duplicated genomes or expanded plant gene families.
- Require both query and target coverage when transferring gene names or functions.
- Check conserved domains for proteins with partial hits or low complexity regions.
- Treat multiple near-tie hits as paralog/isoform ambiguity unless synteny, phylogeny, or curated orthology resolves them.
- For sugarcane or other polyploid crops, expect many homeologs and paralogs; report candidate groups instead of forcing a single ortholog.

## Suggested Thresholds

Use these as starting points, not universal rules:

| Context | Starting threshold |
|---|---|
| close protein homologs | `evalue <= 1e-10`, query and target coverage >= 50% |
| distant protein homologs | `evalue <= 1e-5`, domain consistency required |
| nucleotide/CDS similarity | coverage and synteny matter more than identity alone |
| RBH ortholog candidates | reciprocal top hit, coverage >= 50-70%, no close tie |
| short domains/peptides | report alignment length and domain evidence; avoid strong functional claims |

## Expected Outputs

When useful, produce:

- `homolog_hits.tsv`: filtered homolog candidates.
- `rbh_pairs.tsv`: reciprocal best-hit candidate pairs.
- `ambiguous_hits.tsv`: queries with multiple near-equal hits.
- `no_hits.txt`: queries without passing hits.
- `domain_hits.tsv`: HMMER/Pfam-style domain evidence when used.

Report method, input files, target database, thresholds, number of queries, number with hits, number of RBH pairs or candidate groups, and main uncertainty sources.

## Validation

- Confirm input FASTA molecule type and sequence count.
- Verify local search databases were built from the intended FASTA version.
- Re-run a small subset manually or with stricter thresholds when top hits look unexpected.
- Check whether low-complexity masking changes the top hits.
- For ortholog candidates, validate with at least one independent signal when possible: Ensembl orthology, synteny, domain architecture, or phylogenetic tree.

## Related Skills

- `skills/sequence/search/local-blast` - Local databases and batch searches.
- `skills/sequence/search/remote-blast` - Quick public NCBI BLAST for small non-private queries.
- `UniProt lookup method (not included in this package)` - Protein function for candidate homologs.
- `skills/databases/ensembl/ensembl-database` - Public ortholog and comparative genomics context from Ensembl.

