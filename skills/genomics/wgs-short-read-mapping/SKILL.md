---
name: wgs-short-read-mapping
description: Map paired-end short-read WGS resequencing FASTQ to a reference genome
  with BWA-MEM or BWA-MEM2, samtools, and duplicate marking, producing coordinate-sorted
  indexed BAMs for downstream SNP/InDel, SV, and CNV calling. Use before WGS variant
  calling when users provide FASTQ rather than ready BAM files. Do not use for RNA-seq,
  long-read, or assembly comparison data.
version: 1.2.0
tags:
- WGS
- resequencing
- mapping
- BWA
- BWA-MEM2
- samtools
- Picard
- sambamba
- mark-duplicates
- BAM
legacy_ids:
- short-read-mapping
- bwa-mapping
- wgs-bwa-mem
- resequencing-mapping
- 双端WGS fastq
- WGS fastq比对
- 先比对再做GATK
task_types:
- wgs_mapping
stage_tags:
- read_mapping
- bam_preparation
inputs:
- reference_fasta
- paired_fastq_sample_table
- output_directory
outputs:
- sorted_markdup_bam
- bam_index
- flagstat
- coverage
- duplicate_metrics
- bam_list
generate:
  script_invocations:
    scripts/inspect_wgs_inputs.py:
      schema_version: 1
      runtime: controller_python
      arguments:
        --fastq:
          nargs: 2
          required: true
          repeatable: true
        --reference:
          nargs: 2
          required: true
        --observation-id:
          nargs: 1
          required: true
        --evidence-dir:
          nargs: 1
          required: true
          runtime_environment_variable: BIOMIND_PREFLIGHT_EVIDENCE_DIR
        --item-timeout-seconds:
          nargs: 1
      delegates:
      - scripts/inspect_fastq.py
      - scripts/inspect_fasta_header.py
      - scripts/inspect_fastq_batch.py
    scripts/inspect_fastq_batch.py:
      schema_version: 1
      runtime: controller_python
      arguments:
        --item:
          nargs: 2
          required: true
          repeatable: true
        --observation-id:
          nargs: 1
          required: true
        --evidence-dir:
          nargs: 1
          required: true
          runtime_environment_variable: BIOMIND_PREFLIGHT_EVIDENCE_DIR
        --item-timeout-seconds:
          nargs: 1
        --records:
          nargs: 1
        --input-bytes:
          nargs: 1
        --decoded-bytes:
          nargs: 1
        --seconds:
          nargs: 1
      delegates:
      - scripts/inspect_fastq.py
    scripts/inspect_fastq.py:
      schema_version: 1
      runtime: controller_python
      arguments:
        --path:
          nargs: 1
          required: true
        --observation-id:
          nargs: 1
          required: true
        --records:
          nargs: 1
        --input-bytes:
          nargs: 1
        --decoded-bytes:
          nargs: 1
        --seconds:
          nargs: 1
    scripts/inspect_fasta_header.py:
      schema_version: 1
      runtime: controller_python
      arguments:
        --path:
          nargs: 1
          required: true
        --observation-id:
          nargs: 1
          required: true
        --header-bytes:
          nargs: 1
        --input-bytes:
          nargs: 1
        --seconds:
          nargs: 1
  diagnostic_operations:
  - operation_id: samtools_header
    description: Read the SAM/BAM header through the already approved samtools runtime.
      This does not validate every alignment.
    read_only: true
    argv:
    - samtools
    - view
    - -H
    - '{input:0}'
    limits:
      max_wall_seconds: 30
      max_threads: 1
      max_input_bytes: 67108864
      max_output_bytes: 65536
      max_files: 1
  - operation_id: binary_prefix
    description: Inspect at most 64 bytes with the existing od program when a built-in
      text reader cannot represent an approved object.
    read_only: true
    argv:
    - od
    - -An
    - -tx1
    - -N64
    - '{input:0}'
    limits:
      max_wall_seconds: 10
      max_threads: 1
      max_input_bytes: 67108864
      max_output_bytes: 4096
      max_files: 1
  execution_capabilities:
  - capability_id: genomics.wgs.short.read.mapping.execute
    operation: produce_files
    contract_source: skills/genomics/wgs-short-read-mapping/SKILL.md#generate.execution_capabilities
    contract_revision: wgs-short-read-mapping-effects-v2
    required_runtime_binding_ids:
    - runtime.bwa.mem.bin
    - runtime.samtools.mapping.bin
    - runtime.picard.markduplicates.bin
    - runtime.java.bin
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
    - Before input checks or mapping, read references/mapping-methods.md from the selected snapshot. Before runtime selection or reference preparation, read references/runtime-and-reference.md. Before publishing failed or partial execution, read references/failure-publication.md. Use skill.resource; these are on-demand materials, not executable paths.
    - Bind each concrete BAM, index, metrics file, or report through step.outputs
      using a distinct candidate-local artifact declaration. generated_artifact names
      the effect kind, not a resource to alias.
    - Read exact TaskSpec inputs or reachable upstream artifacts through step.inputs.
      Keep the scientific command choice in the step title, description, parameters,
      and success criteria.
    - A container directory grants no mutable-workspace authority. These operations
      produce new immutable files or an explicitly declared immutable bundle; they
      do not mutate a shared output directory.
    - Use the exact bound executable paths. For Picard explicitly set JAVA_HOME to
      the parent prefix of JAVA_BIN and PATH to that prefix/bin:/usr/bin:/bin in the
      approved script, or launch the selected Java directly with the verified Picard
      JAR. Ambient JAVA_HOME is not evidence. Read references/runtime-and-reference.md
      before generating the action.
  - capability_id: genomics.wgs.short.read.mapping.inspect
    operation: inspect
    contract_source: skills/genomics/wgs-short-read-mapping/SKILL.md#generate.execution_capabilities
    contract_revision: wgs-short-read-mapping-effects-v2
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
    - Before input checks or mapping, read references/mapping-methods.md from the selected snapshot. Before runtime selection or reference preparation, read references/runtime-and-reference.md. Before publishing failed or partial execution, read references/failure-publication.md. Use skill.resource; these are on-demand materials, not executable paths.
    - A formal input-check Plan step may declare outputs=[] and deliver validation_observation
      directly. Do not require a separate 00.input_check report merely because the
      overall workflow produces files.
    - Downstream reporting must consume the accepted observation from the same approved
      Plan and explicit predecessor dependency. Preserve the inspected scope; a prefix/header
      check is not full-file validation. Required final report, reproducibility script
      and stage status remain separate Plan results.
    - FASTQ batch is FASTQ-only. For FASTQ plus plain reference, use the registered
      inspect_wgs_inputs.py composition with explicit --fastq/--reference, one validation_observation
      and the runtime BIOMIND_PREFLIGHT_EVIDENCE_DIR; helper alone creates its nonexistent
      leaf. Preserve per-item scopes and adverse/incomplete findings.
    - Use for input, reference, or BAM checks that produce no durable output file.
      Emit structured validation_observation evidence with a value, including an adverse
      value when the check fails.
    - Scientific success criteria and the existing verification layer judge the observation;
      the action claim alone does not establish success.
  - capability_id: genomics.wgs.short.read.mapping.prepare_reference
    operation: prepare_reference
    contract_source: skills/genomics/wgs-short-read-mapping/SKILL.md#generate.execution_capabilities
    contract_revision: wgs-short-read-mapping-effects-v2
    required_runtime_binding_ids:
    - runtime.bwa.index.bin
    - runtime.samtools.faidx.bin
    - runtime.picard.dictionary.bin
    - runtime.java.bin
    optional_runtime_binding_ids: []
    required_artifact_binding_ids: []
    produces_artifacts:
    - artifact_id: reference_indexes
      resource_kind: immutable_artifact
    produces_bindings: []
    produces_observations:
    - observation_id: reference_bundle_valid
      predicate:
        predicate_id: genomics.wgs.reference_bundle.v1
        operator: boolean_equals
        expected: true
        parser_id: biomind.genomics.reference_bundle.v1
        failure_disposition: correction_required
        missing_disposition: inspection_required
      evidence_contract:
        required_fields:
        - value
    workspace_mutations: []
    explicit_side_effects:
    - effect_id: create_reference_indexes
      evidence_contract:
        required_fields:
        - reference_resource_ref
        - index_artifacts
    execution_guidance:
    - Before input checks or mapping, read references/mapping-methods.md from the selected snapshot. Before runtime selection or reference preparation, read references/runtime-and-reference.md. Before publishing failed or partial execution, read references/failure-publication.md. Use skill.resource; these are on-demand materials, not executable paths.
    - Declare the reference index files or index bundle in step.outputs and the exact
      source reference in step.inputs. Keep the approved input immutable and place
      new index products only at approved output locations.
    - Report create_reference_indexes with reference_resource_ref and index_artifacts
      identifying the actual products. Planning is a promise; execution, artifact
      validation, and approval still must establish completion.
    - If no index was produced, publish only the observed create_reference_indexes
      side fact with index_artifacts=[] and status=blocked; omit the artifact row.
      Do not create an empty bundle to satisfy publication. This never completes the
      step.
    - Produce exactly one immutable directory artifact containing reference.fa (byte-identical
      copy of the formal reference_fasta input), reference.fa.fai, reference.fa.amb/.ann/.bwt/.pac/.sa
      and reference.dict. The source input is never mutated. Emit reference_bundle_valid.value;
      the deterministic parser independently validates complete indexes, contig lengths
      and dictionary MD5 before Commit. A created directory or partial output never
      satisfies this capability.
    - Use the exact bound executable paths. For Picard explicitly set JAVA_HOME to
      the parent prefix of JAVA_BIN and PATH to that prefix/bin:/usr/bin:/bin in the
      approved script, or launch the selected Java directly with the verified Picard
      JAR. Ambient JAVA_HOME is not evidence. Read references/runtime-and-reference.md
      before generating the action.
  - capability_id: genomics.wgs.short.read.mapping.coverage
    operation: inspect
    contract_source: skills/genomics/wgs-short-read-mapping/SKILL.md#generate.execution_capabilities
    contract_revision: wgs-short-read-mapping-effects-v2
    required_runtime_binding_ids:
    - runtime.samtools.coverage.bin
    optional_runtime_binding_ids: *id001
    required_artifact_binding_ids: *id002
    produces_artifacts: *id003
    produces_bindings: *id004
    produces_observations: *id005
    workspace_mutations: *id006
    explicit_side_effects: *id007
    execution_guidance:
    - Before input checks or mapping, read references/mapping-methods.md from the selected snapshot. Before runtime selection or reference preparation, read references/runtime-and-reference.md. Before publishing failed or partial execution, read references/failure-publication.md. Use skill.resource; these are on-demand materials, not executable paths.
    - Use only for samtools coverage after its actual subcommand probe passed. Missing
      coverage is a visible capability gap; do not silently replace it with depth
      or upgrade the environment.
runtime:
  default_env: bio_wgs
  env_runner: bash
  language: bash
  packages:
  - bwa
  - samtools
  - picard
  capabilities:
  - short-read-alignment
  - duplicate-marking
  - bam-qc
  install_policy: prefer-declared
  install_command: mamba create -n bio_wgs -c conda-forge -c bioconda -y bwa samtools
    picard
  tools:
  - bwa
  - samtools
  - picard
  - java
  inspection_probes:
  - output: RESOLVED_BWA_INDEX_BIN
    value: '{prefix}/bin/bwa'
    checks:
    - argv:
      - '{prefix}/bin/bwa'
      - index
      success_codes:
      - 1
      contains:
      - 'Usage:'
      - bwa index
  - output: RESOLVED_BWA_MEM_BIN
    value: '{prefix}/bin/bwa'
    checks:
    - argv:
      - '{prefix}/bin/bwa'
      - mem
      success_codes:
      - 1
      contains:
      - 'Usage:'
      - bwa mem
  - output: RESOLVED_SAMTOOLS_FAIDX_BIN
    value: '{prefix}/bin/samtools'
    checks:
    - argv:
      - '{prefix}/bin/samtools'
      - faidx
      success_codes:
      - 255
      contains:
      - 'Usage:'
      - faidx
  - output: RESOLVED_SAMTOOLS_MAPPING_BIN
    value: '{prefix}/bin/samtools'
    checks:
    - argv:
      - '{prefix}/bin/samtools'
      - sort
      - --help
      success_codes:
      - 0
      - 1
      contains:
      - sort
    - argv:
      - '{prefix}/bin/samtools'
      - index
      - --help
      success_codes:
      - 0
      - 1
      contains:
      - index
    - argv:
      - '{prefix}/bin/samtools'
      - flagstat
      - --help
      success_codes:
      - 0
      - 1
      contains:
      - flagstat
    - argv:
      - '{prefix}/bin/samtools'
      - depth
      - --help
      success_codes:
      - 0
      - 1
      contains:
      - depth
    - argv:
      - '{prefix}/bin/samtools'
      - quickcheck
      - --help
      success_codes:
      - 0
      - 1
      contains:
      - quickcheck
  - output: RESOLVED_SAMTOOLS_COVERAGE_BIN
    value: '{prefix}/bin/samtools'
    checks:
    - argv:
      - '{prefix}/bin/samtools'
      - coverage
      - --help
      success_codes:
      - 0
      - 1
      contains:
      - 'Usage:'
      - coverage
  - output: RESOLVED_PICARD_DICTIONARY_BIN
    value: '{prefix}/bin/picard'
    checks:
    - argv:
      - '{prefix}/bin/picard'
      - CreateSequenceDictionary
      - --help
      success_codes:
      - 0
      - 1
      contains:
      - CreateSequenceDictionary
      - REFERENCE
  - output: RESOLVED_PICARD_MARKDUPLICATES_BIN
    value: '{prefix}/bin/picard'
    checks:
    - argv:
      - '{prefix}/bin/picard'
      - MarkDuplicates
      - --help
      success_codes:
      - 0
      - 1
      contains:
      - MarkDuplicates
      - INPUT
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
---

# WGS short-read mapping

Convert paired FASTQ to sorted, duplicate-marked BAMs; mark and keep duplicates for calling unless the approved method says otherwise. The metadata above is the authoritative helper, runtime and StepEffect contract.

## Required phase reads

Use the existing skill.resource/read interface on the selected snapshot:
- Before input validation or mapping: references/mapping-methods.md (sample/read groups, reference identity, thresholds and method parameters).
- Before prepare_reference or choosing Java/launchers: references/runtime-and-reference.md (fixed complete bundle layout and actual runtime requirements).
- Before reporting failed/partial execution: references/failure-publication.md (native returncode, structured observations and side facts).

## Planning and execution rules

Use .inspect for controller helper input checks, .prepare_reference for one complete immutable index bundle, .execute for BWA/Picard mapping products, and .coverage only if its samtools coverage probe passed. Explicit inputs/outputs and producer dependencies remain required. No installer, environment mutation or unapproved substitute belongs in scientific code. Dependency preparation can be a separate approved installation work order; its completion resumes this scientific step and cannot mark it passed.

Do not infer assembly identity from matching contig names. Confirm read groups, ploidy-related downstream decisions, scientific thresholds and duplicate policy from the task. No reference or FASTQ mutation. Preserve existing output files.

Declare only a selected helper root handle; the binder expands its explicit delegates from this same snapshot. Use the provided entrypoint and business arguments. The approved preflight evidence leaf is created by the helper after approval, never pre-created by Generate.

A directory existing, a zero exit code or a created message alone is insufficient. prepare_reference must satisfy the declared complete-bundle predicate and the normal Supervisor/Commit path. Missing or incompatible runtime facts remain unknown until inspected; use the existing Recovery/Plan revision process.
