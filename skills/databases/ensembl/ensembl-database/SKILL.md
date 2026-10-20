---
name: ensembl-database
description: Query the Ensembl genome database through the Ensembl REST API for 250+ Ensembl and Ensembl Genomes
  species. Use for gene lookup, sequence retrieval, variant analysis, VEP consequence prediction, comparative genomics,
  orthologs, paralogs, region annotation, and assembly coordinate mapping. Use this as a public database supplement,
  not for project-specific local genome IDs or custom assemblies absent from Ensembl.
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

## 联网与结构化结果核验

首次联网前读取 [公共数据库联网与结构化结果核验](../../references/network-and-structured-retrieval.md)，同一会话已读且未变时复用。先确认当前可用的官方 API 或插件路径，再按下列领域工作流执行；复用当前工具 schema 已提供的原始响应模式；搜索摘要和未完整回收的正文不能替代结构化记录。

# Ensembl Database Query

Use this skill to query public Ensembl genome resources through REST calls. Prefer a verified transport that preserves the official response and exact endpoint. Existing `requests` calls remain suitable; use `ensembl_rest` only when already available or explicitly preferred. If this direct path fails, assess an already configured plugin path only if it can preserve the required raw format; a search summary is not REST JSON.

## Scope

- Gene annotation: Ensembl stable IDs, gene symbols, coordinates, strand, biotype, transcripts, translations, and external references such as UniProt or RefSeq.
- Sequence retrieval: genomic DNA, cDNA, CDS, transcript, protein, and region sequences.
- Variant analysis: known variants by rsID or COSMIC ID, regional variants, and VEP predicted consequences.
- Comparative genomics: orthologs, paralogs, homology, gene trees, and family context.
- Region annotation: genes, transcripts, exons, CDS, regulatory elements, and variants in `chromosome:start-end` intervals.
- Coordinate conversion: assembly-to-assembly mapping such as GRCh37/hg19 to GRCh38 where Ensembl supports the mapping.

## Workflow

1. Normalize the species name to the Ensembl REST convention, usually lowercase binomial with underscores, for example `homo_sapiens`, `mus_musculus`, `oryza_sativa`, `zea_mays`, or `arabidopsis_thaliana`.
2. Confirm species availability before plant, microbial, or non-model queries with `/info/species?division=...` or `/info/genomes/{genome_name}`.
3. Select the right base server:
   - Current Ensembl and Ensembl Genomes REST: `https://rest.ensembl.org`
   - Human GRCh37/hg19 archive REST: `https://grch37.rest.ensembl.org`
4. Query by the strongest identifier available:
   - Ensembl stable ID: `/lookup/id/{id}?expand=1`
   - Gene symbol: `/lookup/symbol/{species}/{symbol}?expand=1`
   - Region: `/overlap/region/{species}/{region}?feature=gene;feature=transcript`
   - Variant ID: `/variation/{species}/{variant_id}` or `/vep/{species}/id/{variant_id}`
5. Return a compact, evidence-oriented summary: stable ID, display name, species, assembly, coordinates, strand, biotype, transcript/protein IDs, consequence terms, ortholog target species, and the exact endpoint URL used.
6. For batch queries, prefer POST endpoints where Ensembl provides them and keep batches modest to avoid rate-limit or timeout problems.

## Common REST Patterns

```python
import requests

server = "https://rest.ensembl.org"
headers = {"Content-Type": "application/json", "Accept": "application/json"}

def ensembl_get(endpoint):
    response = requests.get(server + endpoint, headers=headers, timeout=(10, 30))
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError("Ensembl returned an error payload")
    return payload

gene = ensembl_get("/lookup/symbol/homo_sapiens/BRCA2?expand=1")
print(gene["id"], gene["seq_region_name"], gene["start"], gene["end"])
```

Use these endpoint families as the first choices:

- Species metadata: `/info/species?division=EnsemblPlants`, `/info/genomes/{genome_name}`
- Gene lookup: `/lookup/id/{ensembl_id}?expand=1`, `/lookup/symbol/{species}/{symbol}?expand=1`
- Cross references: `/xrefs/id/{ensembl_id}`, `/xrefs/symbol/{species}/{symbol}`
- Sequences: `/sequence/id/{id}?type=genomic`, `/sequence/id/{id}?type=cdna`, `/sequence/id/{id}?type=protein`, `/sequence/region/{species}/{region}`
- Variants: `/variation/{species}/{variant_id}`, `/overlap/region/{species}/{region}?feature=variation`
- VEP: `/vep/{species}/id/{variant_id}`, `/vep/{species}/region/{region}/{allele}`, POST `/vep/{species}/id`
- Comparative genomics: `/homology/id/{species}/{id}`, `/homology/symbol/{species}/{symbol}`, `/genetree/id/{id}`. For current homology-by-ID requests include species; validate archive-specific routes against that server rather than reusing an obsolete path.
- Region annotation: `/overlap/region/{species}/{region}?feature=gene;feature=transcript;feature=exon;feature=cds;feature=regulatory;feature=variation`
- Assembly mapping: `/map/{species}/{source_assembly}/{region}/{target_assembly}`

For plant, fungal, protist, metazoa, or pan-taxonomic comparative queries, add the appropriate `compara` parameter when needed, for example `compara=plants`, `compara=fungi`, `compara=metazoa`, or `compara=pan_homology`.

## Transport and Format Validation

The requests timeout tuple in the example controls connection/read waits, not the total wall-clock operation; use the existing execution runner's total deadline as described in the shared reference. Explicitly request and verify JSON for metadata or FASTA/text for sequences. HTTP 200 with an HTML/YAML presentation is not the requested JSON. For metadata-only homology results, use documented sequence=none and format=condensed where they retain all required fields; request full fields only when needed. Do not dump returned sequences into the model context.

## Output Guidance

- State the Ensembl release or assembly when it is present in the response.
- Distinguish gene, transcript, exon, CDS, and translation coordinates.
- Keep chromosome names exactly as Ensembl reports them; do not silently add or remove `chr`.
- For VEP, report transcript ID, consequence terms, impact, affected protein/cDNA coordinates, amino-acid change, and gene symbol when available.
- For orthologs, include target species, target gene ID/symbol, orthology type, percent identity, and confidence when present.
- For sequence retrieval, include FASTA headers that preserve ID, species, assembly, coordinate range, strand, and sequence type.

## Limitations

- Use this skill only for species and assemblies available in Ensembl or Ensembl Genomes.
- Confirm whether a plant species is available in Ensembl Plants before querying; not every crop or cultivar assembly is hosted.
- Do not use Ensembl lookup for project-specific local IDs such as `GT21.XXgYG######` unless they are known to be registered in Ensembl.
- For local sugarcane GT21 analyses, use local files such as `GT21.final.gff3`, `GT21.final.pep.fasta.gz`, `GT21.gene.function`, and `GT21.emapper.annotations` instead of Ensembl lookup.
- REST VEP uses the current public Ensembl service; for strict reproducibility, record the query date, release, server, endpoint, and request payload.
- For protein function, domains, GO, structures, or curated annotations beyond genome context, follow with UniProt or another protein-focused database.

## Official References

- Ensembl REST endpoint index: `https://rest.ensembl.org/`
- Current homology-by-ID endpoint: `https://rest.ensembl.org/documentation/info/homology_species_gene_id`
- Species metadata endpoint: `https://rest.ensembl.org/documentation/info/species`
- Current Ensembl REST API examples: `https://rest.ensembl.org/documentation/info/lookup`
- GRCh37 REST service: `https://grch37.rest.ensembl.org/`
- Ensembl Genomes REST guidance: `https://plants.ensembl.org/info/data/rest.html`

## Related Skills

