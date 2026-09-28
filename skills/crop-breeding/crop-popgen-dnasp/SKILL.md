---
name: crop-popgen-dnasp
description: Analyze crop candidate genes or genomic intervals with DnaSP-like population genetics statistics from
  FASTA alignments or VCF plus regions and sample groups. Use for nucleotide diversity pi, Tajima's D, haplotype
  diversity, LD r2, Dxy/Da, SFS, Ka/Ks, and selection-signal summaries around GWAS or DEG candidate loci.
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

# Crop PopGen DnaSP

Use this skill for candidate gene/region population genetics in crop breeding:

```text
alignment or VCF + region + sample groups -> pi/TajimaD/LD/haplotypes/Dxy/KaKs -> breeding interpretation report
```

## Inputs

Mode A:

- `--alignment region.fasta`

Mode B:

- `--vcf population.vcf.gz`
- `--region-bed candidate_regions.bed`
- `--sample-group sample_group.tsv`
- `--reference-fasta genome.fa`
- Optional `--gff genome.gff3`

Analysis options: `polymorphism,ld,divergence,kaks,sfs`.

## Workflow

1. Extract candidate regions from VCF:
   `bash "${SKILL_DIR}/scripts/extract_region_from_vcf.sh" --vcf population.vcf.gz --regions candidate_regions.bed --outdir work/regions`
2. Convert VCF to alignment when needed:
   `python "${SKILL_DIR}/scripts/vcf_to_fasta_alignment.py" --vcf work/regions/region.vcf.gz --reference genome.fa --output region.fasta`
3. Compute statistics:
   `python "${SKILL_DIR}/scripts/run_popgen_stats.py" --alignment region.fasta --sample-group sample_group.tsv --outdir results/popgen --window-size 10000 --step-size 5000`
4. Prepare haplotype network input:
   `python "${SKILL_DIR}/scripts/haplotype_network_input.py" --alignment region.fasta --output haplotype_network_input.tsv`
5. Summarize candidate region:
   `python "${SKILL_DIR}/scripts/summarize_candidate_region.py" --stats results/popgen/polymorphism_stats.tsv --outdir results/popgen`

## Outputs

- `tables/polymorphism_stats.tsv`
- `tables/tajimaD_by_region.tsv`
- `tables/pi_by_group.tsv`
- `tables/fst_or_dxy_by_group.tsv`
- `tables/haplotype_summary.tsv`
- `tables/ld_decay.tsv`
- `tables/kaks_results.tsv`
- `figures/pi_sliding_window.png`
- `figures/tajimaD_sliding_window.png`
- `figures/ld_decay.png`
- `figures/haplotype_network_input.tsv`
- `report.md`, `result.json`, `reproducibility/commands.sh`

## Breeding Interpretation

- Low pi may indicate a selection sweep or reduced diversity.
- Tajima's D < 0 may indicate recent positive selection or expansion.
- Tajima's D > 0 may indicate balancing selection or population structure.
- High Dxy/Da between high-trait and low-trait groups suggests candidate-region differentiation.
- Ka/Ks > 1 can suggest positive selection, but requires careful validation.

## Gotchas

- Do not assume human chromosome names; crop `Chr01`, `Chr1`, and scaffolds are valid.
- For CDS Ka/Ks, check length divisible by 3 and stop codons.
- Interpret population statistics together with sampling design and structure.

## Tests

Validate pi, Tajima's D, haplotypes, and LD outputs with independent validation data or user-provided alignments; do not keep local example population files inside this skill.
