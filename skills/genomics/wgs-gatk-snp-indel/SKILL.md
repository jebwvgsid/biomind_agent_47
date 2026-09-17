---
name: wgs-gatk-snp-indel
description: Call short-read WGS SNPs and InDels with the GATK HaplotypeCaller GVCF
  workflow, including per-sample GVCFs, GenomicsDBImport or CombineGVCFs, joint genotyping,
  hard filtering, validation, and summary counts. Use for high-confidence cohort resequencing
  variant calling from duplicate-marked BAMs. Do not use for RNA-seq, long-read SV,
  CNV, or assembly comparison.
version: 1.2.0
tags:
- WGS
- GATK
- HaplotypeCaller
- GVCF
- GenomicsDBImport
- SNP
- InDel
- joint-genotyping
- hard-filtering
legacy_ids:
- gatk-wgs
- wgs-gatk
- gatk-snp-indel
- resequencing-gatk
- GATK SNP和InDel检测
- GATK SNP InDel
- 双端WGS GATK SNP InDel
- WGS fastq GATK
task_types:
- wgs_snp_indel_calling
stage_tags:
- gatk_haplotypecaller
- joint_genotyping
- hard_filtering
inputs:
- reference_fasta
- sorted_markdup_bam
- bam_list
- interval_plan
outputs:
- gvcf
- raw_vcf
- filtered_snp_vcf
- filtered_indel_vcf
- stats
generate:
  execution_capabilities:
  - capability_id: genomics.wgs.gatk.snp.indel.execute
    operation: produce_files
    contract_source: skills/genomics/wgs-gatk-snp-indel/SKILL.md#generate.execution_capabilities
    contract_revision: wgs-gatk-snp-indel-effects-v2
    required_runtime_binding_ids:
    - runtime.gatk.bin
    - runtime.python.bin
    - runtime.java.bin
    - runtime.samtools.bin
    - runtime.bcftools.bin
    optional_runtime_binding_ids: []
    required_artifact_binding_ids: []
    produces_artifacts:
    - artifact_id: generated_artifact
      resource_kind: immutable_artifact
    produces_bindings: []
    produces_observations: []
    workspace_mutations: []
    explicit_side_effects: []
    execution_guidance:
    - Before planning biological parameters or generating calling, joint-genotyping, filtering or validation, read references/calling-methods.md from the selected snapshot with skill.resource.
    - If ploidy or filter applicability remains a required unresolved user decision,
      use the existing ask_user/blocked outcome before calling; Plan approval alone
      does not confirm it. Pass the explicitly confirmed sample ploidy to HaplotypeCaller.
    - Bind each concrete GVCF, combined GVCF, VCF, index, or statistics file through
      step.outputs using a distinct candidate-local artifact declaration. generated_artifact
      names an effect kind, not a resource to alias.
    - Read actual upstream products through step.inputs and explicit producer dependencies.
      GenotypeGVCFs consumes a combined GVCF or a declared GenomicsDB bundle; bcftools
      stats consumes the actual VCF it reports.
    - Use a new explicitly declared immutable directory artifact for a one-shot GenomicsDB
      bundle. An output container does not authorize incremental workspace mutations
      or supply another file's state.
    - This revision selects CombineGVCFs (not GenomicsDBImport). Use the exact PYTHON_BIN
      to invoke GATK_BIN; its env-python shebang is not the verified launcher. Explicitly
      set PATH to the JAVA_BIN prefix/bin:/usr/bin:/bin and JAVA_HOME to that prefix
      in the approved script. Read references/calling-methods.md for the current phase.
  - capability_id: genomics.wgs.gatk.snp.indel.inspect
    operation: inspect
    contract_source: skills/genomics/wgs-gatk-snp-indel/SKILL.md#generate.execution_capabilities
    contract_revision: wgs-gatk-snp-indel-effects-v2
    required_runtime_binding_ids: []
    optional_runtime_binding_ids: &id001 []
    required_artifact_binding_ids: &id002 []
    produces_artifacts: &id003 []
    produces_bindings: &id004 []
    produces_observations: &id005
    - observation_id: validation_observation
      evidence_contract:
        required_fields:
        - value
    workspace_mutations: &id006 []
    explicit_side_effects: &id007 []
    execution_guidance:
    - Before planning biological parameters or generating calling, joint-genotyping, filtering or validation, read references/calling-methods.md from the selected snapshot with skill.resource.
    - A formal input-check Plan step may declare outputs=[] and deliver validation_observation
      directly. Do not require a separate 00.input_check report merely because the
      overall workflow produces files.
    - Downstream reporting must consume the accepted observation from the same approved
      Plan and explicit predecessor dependency. Preserve the inspected scope; a prefix/header
      check is not full-file validation. Required final report, reproducibility script
      and stage status remain separate Plan results.
    - Use for preflight or variant validation that produces no durable output file.
      Emit structured validation_observation evidence with a value, including an adverse
      value for a failed check.
    - Scientific success criteria and the existing verification layer judge the observation.
      A successful structural evidence match does not by itself validate scientific
      compatibility.
  - capability_id: genomics.wgs.gatk.snp.indel.validate
    operation: inspect
    contract_source: skills/genomics/wgs-gatk-snp-indel/SKILL.md#generate.execution_capabilities
    contract_revision: wgs-gatk-snp-indel-effects-v2
    required_runtime_binding_ids:
    - runtime.gatk.bin
    - runtime.python.bin
    - runtime.java.bin
    optional_runtime_binding_ids: *id001
    required_artifact_binding_ids: *id002
    produces_artifacts: *id003
    produces_bindings: *id004
    produces_observations: *id005
    workspace_mutations: *id006
    explicit_side_effects: *id007
    execution_guidance:
    - Before planning biological parameters or generating calling, joint-genotyping, filtering or validation, read references/calling-methods.md from the selected snapshot with skill.resource.
    - Use the verified Python/GATK/Java combination for ValidateVariants; .inspect
      is only a controller-side input observation.
runtime:
  default_env: bio_gatk
  env_runner: bash
  language: bash
  packages:
  - gatk4
  - samtools
  - bcftools
  capabilities:
  - haplotypecaller
  - gvcf
  - combinegvcfs
  - hard-filtering
  tools:
  - gatk
  - samtools
  - bcftools
  - java
  - python3
  inspection_probes:
  - output: RESOLVED_GATK_BIN
    value: '{prefix}/bin/gatk'
    checks:
    - argv:
      - /usr/bin/python3
      - '{prefix}/bin/gatk'
      - HaplotypeCaller
      - --help
      success_codes:
      - 0
      contains:
      - HaplotypeCaller
    - argv:
      - /usr/bin/python3
      - '{prefix}/bin/gatk'
      - CombineGVCFs
      - --help
      success_codes:
      - 0
      contains:
      - CombineGVCFs
    - argv:
      - /usr/bin/python3
      - '{prefix}/bin/gatk'
      - GenotypeGVCFs
      - --help
      success_codes:
      - 0
      contains:
      - GenotypeGVCFs
    - argv:
      - /usr/bin/python3
      - '{prefix}/bin/gatk'
      - SelectVariants
      - --help
      success_codes:
      - 0
      contains:
      - SelectVariants
    - argv:
      - /usr/bin/python3
      - '{prefix}/bin/gatk'
      - VariantFiltration
      - --help
      success_codes:
      - 0
      contains:
      - VariantFiltration
    - argv:
      - /usr/bin/python3
      - '{prefix}/bin/gatk'
      - ValidateVariants
      - --help
      success_codes:
      - 0
      contains:
      - ValidateVariants
  - output: RESOLVED_PYTHON_BIN
    value: /usr/bin/python3
    checks:
    - argv:
      - /usr/bin/python3
      - --version
      success_codes:
      - 0
      contains:
      - Python 3
  - output: RESOLVED_JAVA_BIN
    value: '{prefix}/bin/java'
    checks:
    - argv:
      - '{prefix}/bin/java'
      - -version
      success_codes:
      - 0
      contains:
      - version
  - output: RESOLVED_SAMTOOLS_BIN
    value: '{prefix}/bin/samtools'
    checks:
    - argv:
      - '{prefix}/bin/samtools'
      - --version
      success_codes:
      - 0
      contains:
      - samtools
  - output: RESOLVED_BCFTOOLS_BIN
    value: '{prefix}/bin/bcftools'
    checks:
    - argv:
      - '{prefix}/bin/bcftools'
      - stats
      - --help
      success_codes:
      - 0
      - 1
      contains:
      - bcftools stats
---

# WGS GATK SNP/InDel calling

The metadata is the authoritative runtime and StepEffect contract. Before planning biological parameters, or generating HaplotypeCaller/joint-genotyping/filtering/validation commands, load references/calling-methods.md through the existing selected-snapshot resource interface.

Confirm sample ploidy, reference assembly, read groups, filtering applicability and thresholds from the task; Plan approval alone does not resolve an unanswered biological question. Use duplicate-marked coordinate-sorted BAMs and formal reference/index inputs. Every GVCF/VCF/index/stat file has its own output and explicit consumer dependency.

This revision selects CombineGVCFs joint calling. GenomicsDB requires a separate approved capability revision. .inspect remains controller-side preflight without scientific runtime requirements; actual GATK ValidateVariants uses .validate. Use absolute verified Python/GATK/Java paths and explicit PATH/JAVA_HOME in the approved action; a broken env-python shebang must not be retried as proof GATK is absent.

Scientific Generate never installs or upgrades. A separate Installer may act only on the exact approved target/work order and must verify and register it before resuming the original science step. Registration is not a scientific Commit. Keep native failure evidence, preserve old files, and route implementation changes through the existing Recovery/Planner/review chain.
