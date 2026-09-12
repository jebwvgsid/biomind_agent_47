---
name: wgs-gatk-snp-indel
description: Call short-read WGS SNPs and InDels with the GATK HaplotypeCaller GVCF workflow, including per-sample GVCFs, GenomicsDBImport or CombineGVCFs, joint genotyping, hard filtering, validation, and summary counts. Use for high-confidence cohort resequencing variant calling from duplicate-marked BAMs. Do not use for RNA-seq, long-read SV, CNV, or assembly comparison.
version: "1.1.0"
tags: [WGS, GATK, HaplotypeCaller, GVCF, GenomicsDBImport, SNP, InDel, joint-genotyping, hard-filtering]
legacy_ids: [gatk-wgs, wgs-gatk, gatk-snp-indel, resequencing-gatk, GATK SNP和InDel检测, GATK SNP InDel, 双端WGS GATK SNP InDel, WGS fastq GATK]
task_types: [wgs_snp_indel_calling]
stage_tags: [gatk_haplotypecaller, joint_genotyping, hard_filtering]
inputs: [reference_fasta, sorted_markdup_bam, bam_list, interval_plan]
outputs: [gvcf, raw_vcf, filtered_snp_vcf, filtered_indel_vcf, stats]
generate:
  execution_capabilities:
    - capability_id: genomics.wgs.gatk.snp.indel.execute
      operation: produce_files
      contract_source: skills/genomics/wgs-gatk-snp-indel/SKILL.md#generate.execution_capabilities
      contract_revision: wgs-gatk-snp-indel-effects-v1
      required_runtime_binding_ids: []
      optional_runtime_binding_ids: []
      required_artifact_binding_ids: []
      produces_artifacts:
        - {artifact_id: generated_artifact, resource_kind: immutable_artifact}
      produces_bindings: []
      produces_observations: []
      workspace_mutations: []
      explicit_side_effects: []
      execution_guidance:
        - Bind each concrete GVCF, combined GVCF, VCF, index, or statistics file through step.outputs using a distinct candidate-local artifact declaration. generated_artifact names an effect kind, not a resource to alias.
        - Read actual upstream products through step.inputs and explicit producer dependencies. GenotypeGVCFs consumes a combined GVCF or a declared GenomicsDB bundle; bcftools stats consumes the actual VCF it reports.
        - Use a new explicitly declared immutable directory artifact for a one-shot GenomicsDB bundle. An output container does not authorize incremental workspace mutations or supply another file's state.
    - capability_id: genomics.wgs.gatk.snp.indel.inspect
      operation: inspect
      contract_source: skills/genomics/wgs-gatk-snp-indel/SKILL.md#generate.execution_capabilities
      contract_revision: wgs-gatk-snp-indel-effects-v1
      required_runtime_binding_ids: []
      optional_runtime_binding_ids: []
      required_artifact_binding_ids: []
      produces_artifacts: []
      produces_bindings: []
      produces_observations:
        - observation_id: validation_observation
          evidence_contract: {required_fields: [value]}
      workspace_mutations: []
      explicit_side_effects: []
      execution_guidance:
        - A formal input-check Plan step may declare outputs=[] and deliver validation_observation directly. Do not require a separate 00.input_check report merely because the overall workflow produces files.
        - Downstream reporting must consume the accepted observation from the same approved Plan and explicit predecessor dependency. Preserve the inspected scope; a prefix/header check is not full-file validation. Required final report, reproducibility script and stage status remain separate Plan results.
        - Use for preflight or variant validation that produces no durable output file. Emit structured validation_observation evidence with a value, including an adverse value for a failed check.
        - Scientific success criteria and the existing verification layer judge the observation. A successful structural evidence match does not by itself validate scientific compatibility.
runtime:
  default_env: bio_gatk
  env_runner: bash
  language: bash
  packages: [gatk4, samtools, bcftools, picard]
  capabilities: [haplotypecaller, gvcf, genomicsdb, hard-filtering]
  healthcheck: bash -lc "gatk --version >/dev/null && bcftools --version >/dev/null && samtools --version >/dev/null && command -v picard >/dev/null"
---

# WGS GATK SNP/InDel Calling

Use this skill for the GATK best-practice style short-read WGS GVCF workflow. For large crop/non-model projects, prefer reusing the existing `genomics/wgs-variant-calling` renderer because it already creates interval plans and reproducible scripts.

## Formal planning contract

The versioned `generate.execution_capabilities` above is the formal effect
declaration. Use `.execute` with `operation: produce_files` for new file/bundle
products and `.inspect` with `operation: inspect` for observation-only checks.
Retain HaplotypeCaller, joint genotyping, filtering, and other scientific choices
in the title, description, parameters, and success criteria. An execution
capability ID is not a resource identity or an alias for its output files.

Assign each new file or one-shot GenomicsDB bundle its own candidate-local ref,
with one producer and explicit consumer dependencies. Reads of combined GVCFs,
GenomicsDB bundles, raw VCFs, and filtered VCFs must name that actual upstream
artifact; producing a different file never establishes its state. Independent
sample products may be independent resources; two writers to one mutable
identity may not be treated as independent merely because sample names differ.

This Skill declares no shared mutable-workspace lifecycle or transitions.
`artifact_kind: directory` describes a bundle's shape, not mutation permission.
Do not turn each output-file creation into a mutation of the container directory
or invent workspace state/revision strings. A real incremental shared database
needs a supported lifecycle and ordered transitions before it can be admitted.
Planning future dependencies does not claim their files already exist or pass
execution validation.

## 确认任务边界

For new FASTQ preflight candidates, inspect and reuse the parent wrapper in
`genomics/wgs-short-read-mapping/scripts/inspect_fastq_batch.py`. Put its exact
bound inputs, execution-log directory and adjustable short-check deadline in
the candidate before approval. Do not hide four captured child checks until
the batch ends or forward their nested effect markers. Its stderr item events
are display facts; preserve prefix-only coverage and UNKNOWN failures. Do not
apply its 60-second initial item limit to HaplotypeCaller or other computation,
or modify a previously approved script.

Use this skill when:

- Input is coordinate-sorted, duplicate-marked WGS BAMs aligned to the same reference.
- BAMs have read groups and indexes.
- The goal is SNP/InDel discovery, usually across a cohort.
- The user wants GATK HaplotypeCaller, GVCF, GenomicsDBImport, joint genotyping, or hard filtering.

Do not use this skill for:

- RNA-seq variants; use `genomics/rnaseq-variant-calling`.
- SV/CNV; use LUMPY/CNVnator/long-read skills.
- BAMs without read groups; go back to mapping/preparation.

For polyploid crops, confirm ploidy and interpretation assumptions before treating diploid genotype filters as final truth.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `REFERENCE_FASTA` | FASTA with `.fai` and `.dict` |
| `BAM_LIST` | `sample<TAB>bam_path` preferred |
| `OUTDIR` | output root |
| `INTERVALS` | chromosomes or interval TSV for large genomes |

Recommended:

| Parameter | Default / rule |
|---|---|
| `THREADS` | 4-8 per job; GATK scaling is limited |
| `MEMORY` | choose per interval and sample count |
| `TMPDIR` | fast local scratch if available |
| `JOINT_MODE` | `GenomicsDBImport` for many samples, `CombineGVCFs` for small cohorts |
| `PLOIDY` | default diploid only if biologically appropriate |

Preflight:

```bash
gatk --version
test -s "${REFERENCE_FASTA}.fai"
test -s "${REFERENCE_FASTA%.*}.dict" || test -s "${REFERENCE_FASTA}.dict"
samtools quickcheck -v $(cut -f2 "${BAM_LIST}")
samtools view -H "$(cut -f2 "${BAM_LIST}" | head -1)" | grep '^@RG'
```

Core per-sample GVCF pattern:

```bash
gatk --java-options "-Xmx${MEMORY} -Djava.io.tmpdir=${TMPDIR}" HaplotypeCaller \
  -R "${REFERENCE_FASTA}" \
  -I "${bam}" \
  -L "${interval}" \
  -ERC GVCF \
  -O "${OUTDIR}/03.variant_calling/gvcf/${sample}/${sample}.${interval}.g.vcf.gz"
```

Joint genotyping:

```bash
gatk GenomicsDBImport \
  --sample-name-map "gvcf.${interval}.map" \
  --genomicsdb-workspace-path "${OUTDIR}/03.variant_calling/genomicsdb.${interval}" \
  -L "${interval}" \
  --reader-threads "${THREADS:-4}" \
  --tmp-dir "${TMPDIR}"

gatk GenotypeGVCFs \
  -R "${REFERENCE_FASTA}" \
  -V "gendb://${OUTDIR}/03.variant_calling/genomicsdb.${interval}" \
  -O "${OUTDIR}/03.variant_calling/${interval}.raw.vcf.gz"
```

For small cohorts, `CombineGVCFs` is acceptable, but do not pass a
`sample<TAB>gvcf_path` sample map as `--variant`. `CombineGVCFs --variant`
expects each value to be an actual GVCF/VCF path. Use repeated `--variant`
arguments or a list containing only GVCF paths, then run `GenotypeGVCFs`:

```bash
gatk CombineGVCFs \
  -R "${REFERENCE_FASTA}" \
  --variant "${OUTDIR}/03.variant_calling/gvcf/S1/S1.g.vcf.gz" \
  --variant "${OUTDIR}/03.variant_calling/gvcf/S2/S2.g.vcf.gz" \
  -O "${OUTDIR}/03.variant_calling/cohort.combined.g.vcf.gz"

gatk GenotypeGVCFs \
  -R "${REFERENCE_FASTA}" \
  -V "${OUTDIR}/03.variant_calling/cohort.combined.g.vcf.gz" \
  -O "${OUTDIR}/03.variant_calling/cohort.raw.vcf.gz"
```

Rule of thumb:

- `sample<TAB>gvcf_path` map -> `GenomicsDBImport --sample-name-map`.
- individual GVCF paths -> `CombineGVCFs --variant ... --variant ...`.
- If GATK reports a path like `S1%09/path/to/S1.g.vcf.gz`, the command passed
  a sample map where a GVCF path was expected; switch to one of the two valid
  patterns above instead of retrying the same shape.

Hard-filter defaults for diploid short-read WGS:

```bash
gatk VariantFiltration -R "${REFERENCE_FASTA}" -V all.raw.snp.vcf.gz \
  --filter-expression "QD < 2.0 || MQ < 40.0 || FS > 60.0 || SOR > 3.0 || MQRankSum < -12.5 || ReadPosRankSum < -8.0" \
  --filter-name SNP_filter -O all.filter.snp.vcf.gz

gatk VariantFiltration -R "${REFERENCE_FASTA}" -V all.raw.indel.vcf.gz \
  --filter-expression "QD < 2.0 || FS > 200.0 || SOR > 10.0 || MQRankSum < -12.5 || ReadPosRankSum < -8.0" \
  --filter-name INDEL_filter -O all.filter.indel.vcf.gz
```

Use `SelectVariants --exclude-filtered` for final PASS-only files.

## 输出结果

Expected outputs:

- `03.variant_calling/gvcf/<sample>/*.g.vcf.gz` plus indexes.
- interval raw VCFs and merged `all.raw.vcf.gz`.
- `04.filter/all.filtered.snp.vcf.gz`.
- `04.filter/all.filtered.indel.vcf.gz`.
- `.tbi` or `.idx` indexes.
- `05.summary/*.bcftools.stats.txt`.
- `05.summary/variant_counts.tsv`.
- `logs/*.log`.

Validation:

```bash
gatk ValidateVariants -R "${REFERENCE_FASTA}" -V "${OUTDIR}/04.filter/all.filtered.snp.vcf.gz"
gatk ValidateVariants -R "${REFERENCE_FASTA}" -V "${OUTDIR}/04.filter/all.filtered.indel.vcf.gz"
bcftools stats "${OUTDIR}/04.filter/all.filtered.snp.vcf.gz" > "${OUTDIR}/05.summary/snp.stats.txt"
bcftools stats "${OUTDIR}/04.filter/all.filtered.indel.vcf.gz" > "${OUTDIR}/05.summary/indel.stats.txt"
```

Report raw and PASS counts, sample count, interval strategy, filter expressions, and ploidy.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| no read groups | GATK cannot track samples | fix BAM with read groups before calling |
| duplicate sample names in GVCFs | joint genotyping will fail or merge wrongly | normalize sample names and RG `SM` |
| GenomicsDB workspace already exists | rerun collision | use new workspace or remove only after user approval |
| contig mismatch | BAM/GVCF/reference differ | re-align or use matching reference |
| zero PASS variants | filters too strict or poor data | inspect raw counts and filter distribution before claiming failure |

Do not concatenate VCF text by hand. Use GATK `MergeVcfs`, Picard, or bcftools-aware tools.

## 相似的 skills

- `genomics/wgs-variant-calling` - existing robust GATK script renderer; prefer for large multi-sample crop runs.
- `genomics/wgs-short-read-mapping` - prepare BAMs from FASTQ.
- `genomics/wgs-bcftools-snp-indel` - faster lightweight SNP/InDel workflow.
- `genomics/rnaseq-variant-calling` - RNA-seq variant workflow.
- `genomics/snpeff-annotation` - annotate final VCF.
