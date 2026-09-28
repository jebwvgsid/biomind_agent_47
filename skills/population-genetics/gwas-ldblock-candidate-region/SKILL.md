---
name: gwas-ldblock-candidate-region
description: Annotate candidate genes around significant GWAS loci and generate local LDblock plus Manhattan/gene-track
  plotting commands with LDBlockShow, ShowLDSVG, and bedtools. Use after GWAS plotting when users ask for lead-SNP
  nearby genes, merged significant windows, LD heatmaps, local Manhattan plots, or gene overlays from GWAS results,
  VCF, and GFF3/GTF annotations.
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

# GWAS LDblock Candidate Region

Use this skill after significant GWAS loci have been identified. It turns lead markers into windows, annotates genes, and prepares one LDblock job per merged window.

## 确认任务边界

Use this skill when:

- Input includes standardized GWAS table `SNP CHR BP P`.
- Significant sites are already available or a threshold is provided.
- The user asks for candidate genes, nearby genes, in-gene hits, LD heatmaps, or local Manhattan plots.
- A VCF and matching GFF3/GTF annotation are available for LD/gene overlays.

Do not run genome-wide LDblock by default. Do not report candidate genes without threshold and window size.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `GWAS_TABLE` | standardized `SNP CHR BP P` |
| `VCF` | same marker set/reference used for GWAS |
| `ANNOTATION` | GFF3/GTF matching reference |
| `OUTDIR` | output root |

Optional:

| Parameter | Default / rule |
|---|---|
| `THRESHOLD` | same as Manhattan plot threshold |
| `WINDOW` | `50000` bp each side unless user changes |
| `CUTLINE` | `-log10(THRESHOLD)` |
| `CHR_MAP` | only if chromosome names differ and mapping is validated |

Significant sites:

```bash
mkdir -p "${OUTDIR}/04.significant_loci" "${OUTDIR}/05.ldblock"
awk -v p="${THRESHOLD}" 'NR==1 || $4 < p' "${GWAS_TABLE}" \
  > "${OUTDIR}/04.significant_loci/significant_sites.tsv"
```

Merged windows:

```bash
awk -v w="${WINDOW:-50000}" 'NR>1 {s=$3-w; if(s<0)s=0; print $2"\t"s"\t"$3+w"\t"$1"\t"$4}' \
  "${OUTDIR}/04.significant_loci/significant_sites.tsv" |
sort -k1,1 -k2,2n \
  > "${OUTDIR}/04.significant_loci/significant_sites.windows.bed"

bedtools merge -i "${OUTDIR}/04.significant_loci/significant_sites.windows.bed" \
  > "${OUTDIR}/04.significant_loci/significant_sites.windows.merged.bed"
```

Candidate genes:

```bash
awk '$3=="gene"' "${ANNOTATION}" > "${OUTDIR}/04.significant_loci/genes.gff3"
bedtools intersect -wo \
  -a "${OUTDIR}/04.significant_loci/significant_sites.windows.merged.bed" \
  -b "${OUTDIR}/04.significant_loci/genes.gff3" \
  > "${OUTDIR}/04.significant_loci/candidate_genes.tsv"
```

Generate LDblock jobs:

```bash
python "${SKILL_DIR}/scripts/make_ldblock_jobs.py" \
  --windows "${OUTDIR}/04.significant_loci/significant_sites.windows.merged.bed" \
  --vcf "${VCF}" \
  --gwas "${GWAS_TABLE}" \
  --gff "${ANNOTATION}" \
  --outdir "${OUTDIR}/05.ldblock" \
  --cutline "${CUTLINE}" \
  --script "${OUTDIR}/05.ldblock/run_ldblock.sh"
```

## 输出结果

Expected outputs:

- `04.significant_loci/significant_sites.tsv`
- `04.significant_loci/significant_sites.windows.merged.bed`
- `04.significant_loci/candidate_genes.tsv`
- `04.significant_loci/significant_sites.in_genes.tsv`
- `05.ldblock/run_ldblock.sh`
- `05.ldblock/*.png` or `*.svg` after running LDBlockShow.

Validation:

```bash
test -s "${OUTDIR}/04.significant_loci/significant_sites.windows.merged.bed"
test -s "${OUTDIR}/05.ldblock/run_ldblock.sh"
```

Check chromosome names in GWAS table, VCF, and annotation before plotting.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| no significant sites | no candidate windows | report no-hit and stop or lower threshold only if user asks |
| VCF/GFF chromosomes differ | LD/gene overlay invalid | apply explicit validated chromosome map |
| LDblock run over whole genome | too expensive/unfocused | restrict to merged significant windows |
| candidate genes reported without threshold | not reproducible | record threshold and window size |
| LDBlockShow missing | plotting dependency absent | generate script and report blocked |

## 相似的 skills

- `population-genetics/gwas-manhattan-qq-plot` - creates significant-site table.
- `population-genetics/gwas-emmax-association` - EMMAX association source.
- `population-genetics/gwas-tassel-glm-mlm` - TASSEL association source.
- `crop-breeding/crop-popgen-dnasp` - population genetic follow-up around candidate intervals.
- `genomics/snpeff-annotation` - functional annotation of VCF variants.
