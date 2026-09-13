---
name: wgs-short-read-mapping
description: Map paired-end short-read WGS resequencing FASTQ to a reference genome with BWA-MEM or BWA-MEM2, samtools, and duplicate marking, producing coordinate-sorted indexed BAMs for downstream SNP/InDel, SV, and CNV calling. Use before WGS variant calling when users provide FASTQ rather than ready BAM files. Do not use for RNA-seq, long-read, or assembly comparison data.
version: "1.1.1"
tags: [WGS, resequencing, mapping, BWA, BWA-MEM2, samtools, Picard, sambamba, mark-duplicates, BAM]
legacy_ids: [short-read-mapping, bwa-mapping, wgs-bwa-mem, resequencing-mapping, 双端WGS fastq, WGS fastq比对, 先比对再做GATK]
task_types: [wgs_mapping]
stage_tags: [read_mapping, bam_preparation]
inputs: [reference_fasta, paired_fastq_sample_table, output_directory]
outputs: [sorted_markdup_bam, bam_index, flagstat, coverage, duplicate_metrics, bam_list]
generate:
  diagnostic_operations:
    - operation_id: samtools_header
      description: Read the SAM/BAM header through the already approved samtools runtime. This does not validate every alignment.
      read_only: true
      argv: [samtools, view, -H, "{input:0}"]
      limits: {max_wall_seconds: 30, max_threads: 1, max_input_bytes: 67108864, max_output_bytes: 65536, max_files: 1}
    - operation_id: binary_prefix
      description: Inspect at most 64 bytes with the existing od program when a built-in text reader cannot represent an approved object.
      read_only: true
      argv: [od, -An, -tx1, -N64, "{input:0}"]
      limits: {max_wall_seconds: 10, max_threads: 1, max_input_bytes: 67108864, max_output_bytes: 4096, max_files: 1}
  execution_capabilities:
    - capability_id: genomics.wgs.short.read.mapping.execute
      operation: produce_files
      contract_source: skills/genomics/wgs-short-read-mapping/SKILL.md#generate.execution_capabilities
      contract_revision: wgs-short-read-mapping-effects-v1
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
        - Bind each concrete BAM, index, metrics file, or report through step.outputs using a distinct candidate-local artifact declaration. generated_artifact names the effect kind, not a resource to alias.
        - Read exact TaskSpec inputs or reachable upstream artifacts through step.inputs. Keep the scientific command choice in the step title, description, parameters, and success criteria.
        - A container directory grants no mutable-workspace authority. These operations produce new immutable files or an explicitly declared immutable bundle; they do not mutate a shared output directory.
    - capability_id: genomics.wgs.short.read.mapping.inspect
      operation: inspect
      contract_source: skills/genomics/wgs-short-read-mapping/SKILL.md#generate.execution_capabilities
      contract_revision: wgs-short-read-mapping-effects-v1
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
        - FASTQ batch is FASTQ-only. For FASTQ plus plain reference, use the registered inspect_wgs_inputs.py composition with explicit --fastq/--reference, one validation_observation and the runtime BIOMIND_PREFLIGHT_EVIDENCE_DIR; helper alone creates its nonexistent leaf. Preserve per-item scopes and adverse/incomplete findings.
        - Use for input, reference, or BAM checks that produce no durable output file. Emit structured validation_observation evidence with a value, including an adverse value when the check fails.
        - Scientific success criteria and the existing verification layer judge the observation; the action claim alone does not establish success.
    - capability_id: genomics.wgs.short.read.mapping.prepare_reference
      operation: prepare_reference
      contract_source: skills/genomics/wgs-short-read-mapping/SKILL.md#generate.execution_capabilities
      contract_revision: wgs-short-read-mapping-effects-v1
      required_runtime_binding_ids: []
      optional_runtime_binding_ids: []
      required_artifact_binding_ids: []
      produces_artifacts:
        - {artifact_id: reference_indexes, resource_kind: immutable_artifact}
      produces_bindings: []
      produces_observations: []
      workspace_mutations: []
      explicit_side_effects:
        - effect_id: create_reference_indexes
          evidence_contract:
            required_fields: [reference_resource_ref, index_artifacts]
      execution_guidance:
        - Declare the reference index files or index bundle in step.outputs and the exact source reference in step.inputs. Keep the approved input immutable and place new index products only at approved output locations.
        - Report create_reference_indexes with reference_resource_ref and index_artifacts identifying the actual products. Planning is a promise; execution, artifact validation, and approval still must establish completion.
runtime:
  default_env: bio_wgs
  env_runner: bash
  language: bash
  packages: [bwa, bwa-mem2, samtools, picard, sambamba]
  capabilities: [short-read-alignment, duplicate-marking, bam-qc]
  healthcheck: bash -lc "samtools --version >/dev/null && (bwa 2>&1 | head -1 >/dev/null || bwa-mem2 version >/dev/null) && (command -v picard >/dev/null || command -v sambamba >/dev/null)"
  install_policy: prefer-declared
  install_command: mamba create -n bio_wgs -c conda-forge -c bioconda -y bwa bwa-mem2 samtools picard sambamba
---

# WGS Short-Read Mapping

Use this skill to convert paired-end WGS FASTQ into duplicate-marked coordinate-sorted BAMs. The downstream caller decides whether duplicates should be marked or removed; for variant calling, default to **mark and keep**.

## Formal planning contract

The versioned `generate.execution_capabilities` above is the formal effect
declaration. Use `.execute` with `operation: produce_files` for independently
produced files, `.inspect` with `operation: inspect` for observation-only checks,
and `.prepare_reference` for the declared index-producing operation. Preserve
the scientific operation in each step's semantic fields. Capability IDs select
execution authority; they are never `resource_binding` aliases for new files.

Create one candidate-local declaration for each new immutable artifact and
reuse that local ref in producer `outputs` and consumer `inputs`. Existing
resources must use exact current catalog references. A directory used merely
to contain those files is not a shared mutable object. This Skill declares no
shared-workspace lifecycle or transitions; do not invent workspace state names
or contract revisions. A genuinely incremental mutable workflow needs an
explicit supported lifecycle/transition declaration before it can be planned.
No upstream artifact needs to exist at planning time, but every read must have
an explicit dependency path to its producer or a trusted initial resource.

## 确认任务边界

Use this skill when:

- Input is Illumina/MGI/DNBSEQ-style paired-end WGS or resequencing FASTQ.
- The next stage is WGS SNP/InDel, Manta short-read SV, or CNVnator CNV.
- The user needs reference indexes and BAM QC.

Do not use this skill when:

- Reads are RNA-seq; use `genomics/rnaseq-variant-calling`.
- Reads are ONT/PacBio long reads; use `genomics/long-read-sv-sniffles2`.
- BAMs are already aligned, coordinate-sorted, indexed, and have read groups; go directly to the caller.

Before running, verify that FASTQ files are real project inputs, not tutorial/demo files.

For an approved **prefix inspection**, use this Skill's `scripts/inspect_fastq.py`
with the concrete script path obtained through Skill inspection, the exact bound
input `--path`, and an explicit `--observation-id` from the current StepEffect.
The helper uses the shared bounded inspection reader and emits the versioned
canonical observation. It does not infer identity from a value or certify full
gzip integrity, every read, or all mate pairs. Its record/input/decoded-byte/time
limits and actual coverage are part of the result. A task requiring full checks
still requires those checks; a prefix is evidence for investigation only.

For a **new FASTQ-only batch candidate**, use `scripts/inspect_fastq_batch.py`.
The correct interface is repeated `--item LABEL "BOUND_PATH"` (two arguments).
`--item "LABEL BOUND_PATH"` is invalid: never combine the label and path.
Reference FASTA does not belong in this FASTQ batch, regardless of extension.

For a step checking FASTQ **and** a reference, explicitly choose
`scripts/inspect_wgs_inputs.py`: repeat `--fastq LABEL "BOUND_PATH"`, then pass
`--reference LABEL "BOUND_PATH"`. This thin composition delegates to the two
readers and emits ONE current StepEffect `--observation-id` result, with per-item
scopes and raw logs. It never overwrites four FASTQ observations with a fifth.
`scripts/inspect_fasta_header.py` checks plain UTF-8 FASTA header and one sequence
byte, default header limit 4096 bytes, input limit 8192 bytes, 2 seconds. It does
not scan a long sequence line, all contigs, or the full reference; gzip reference
is explicitly unsupported by this entry. Over-limit headers remain incomplete.
FASTQ retains its existing prefix and gzip bounds; no full-file claim is valid.

Resolve these resources through the selected Skill catalog. For the composition,
declare all four handles in `skill_resources` so approval pins the wrapper and
its delegated resources: `scripts/inspect_wgs_inputs.py`, `scripts/inspect_fastq.py`,
`scripts/inspect_fasta_header.py`, `scripts/inspect_fastq_batch.py`, each prefixed
by this Skill's exact ID and `#`. Use their supplied environment handles, not
an installation root guessed from cwd. Read actual paths from current input
bindings; labels and paths are distinct shell arguments, including spaces/中文.

Pass `--evidence-dir "$BIOMIND_PREFLIGHT_EVIDENCE_DIR"`. The execution policy
approves the existing internal attempt-log root and this controlled reference;
the runtime resolves a NEW leaf for the actual attempt after approval and records
its ownership. **The helper alone creates the final leaf.** Do not assign this
variable, mkdir it, use `mktemp -d`, reuse a previous path, or invent an attempt ID.
The leaf is internal evidence, not another required Plan output. Standalone
approved invocations must likewise use a previously nonexistent approved leaf.
The batch parent emits progress only at actual item/spawn/return boundaries.
Child markers remain in exact raw logs; the parent emits one combined marker.
Check all per-item findings and unstarted entries, not only rc=0. An adverse or
technical result is not a successful preflight; preserve its partial evidence.

During local Generate/Recovery correction, preserve the bound inputs and the
approved checking goals. Do not mechanically freeze a mistaken parser choice,
argument grouping, or historical diagnostic path. Correct the current candidate
and use the existing approval/repair flow; do not edit a prior approved script.

`--item-timeout-seconds` defaults to 60 seconds per short inspection (adjustable
on the new approved candidate). The reader's existing `--seconds 2` limit bounds
cooperative prefix reads; it cannot interrupt a blocked OS read. The wrapper's
local deadline includes interpreter/inspection startup and runs only after
approval; the outer executor's existing wall/stall policy still applies. It
handles TERM/INT through managed process-group cancellation and preserves raw
partial logs and UNKNOWN unfinished checks. This is not a timeout or retry
policy for BWA, GATK or other long scientific computation. A process return is
not a passed check; an invalid observed prefix remains adverse evidence, and
technical failure, timeout or cancellation never certifies full-file integrity.

Do not implement nonempty FASTQ detection as `zcat | head | grep -q` under
`pipefail`: early downstream termination can cause SIGPIPE after a valid header
was found. Do not ignore every 141 or disable pipefail. Empty/invalid observed
data, unsupported representation, decoding errors, and resource limits have
different meanings. Reader failures must retain their technical errors.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `REFERENCE_FASTA` | reference genome FASTA |
| `SAMPLE_TABLE` | TSV with `sample fq1 fq2` |
| `OUTDIR` | output directory |

Recommended:

| Parameter | Default | Notes |
|---|---|---|
| `ALIGNER` | `bwa-mem2` if available, else `bwa mem` | keep command in report |
| `THREADS` | 8 or cluster-provided value | mapping and sorting |
| `SORT_MEM` | `1G` per thread | reduce on memory-limited nodes |
| `PLATFORM` | `illumina` | read group `PL` |
| `DUPLICATE_TOOL` | `picard` or `sambamba` | keep duplicates unless user asks removal |

Sample table:

```tsv
sample	fq1	fq2
S1	/path/S1_R1.fq.gz	/path/S1_R2.fq.gz
S2	/path/S2_R1.fq.gz	/path/S2_R2.fq.gz
```

Core command pattern:

```bash
bwa mem -t "${THREADS}" \
  -R "@RG\tID:${sample}\tSM:${sample}\tPL:${PLATFORM:-illumina}\tLB:${sample}\tPU:${sample}" \
  "${REFERENCE_FASTA}" "${fq1}" "${fq2}" |
samtools sort -@ "${THREADS}" -m "${SORT_MEM:-1G}" \
  -o "${OUTDIR}/02.mapping/${sample}.sort.bam" -

picard MarkDuplicates \
  I="${OUTDIR}/02.mapping/${sample}.sort.bam" \
  O="${OUTDIR}/02.mapping/${sample}.sort.markdup.bam" \
  M="${OUTDIR}/02.mapping/${sample}.markdup.metrics.txt" \
  CREATE_INDEX=true \
  REMOVE_DUPLICATES=false
```

Build missing reference indexes only when needed:

```bash
samtools faidx "${REFERENCE_FASTA}"
bwa index "${REFERENCE_FASTA}"
picard CreateSequenceDictionary R="${REFERENCE_FASTA}" O="${REFERENCE_FASTA%.*}.dict"
```

## 输出结果

Expected files:

- `02.mapping/<sample>.sort.markdup.bam`
- `02.mapping/<sample>.sort.markdup.bam.bai`
- `02.mapping/<sample>.markdup.metrics.txt`
- `02.mapping/<sample>.flagstat.txt`
- `02.mapping/<sample>.coverage.txt`
- `logs/<sample>.bwa.log`
- `bam.list`, preferably `sample<TAB>bam_path`

Validation:

```bash
samtools quickcheck -v "${OUTDIR}/02.mapping/"*.bam
samtools view -H "${OUTDIR}/02.mapping/${sample}.sort.markdup.bam" | grep '^@RG'
samtools flagstat "${OUTDIR}/02.mapping/${sample}.sort.markdup.bam" > "${OUTDIR}/02.mapping/${sample}.flagstat.txt"
samtools coverage "${OUTDIR}/02.mapping/${sample}.sort.markdup.bam" > "${OUTDIR}/02.mapping/${sample}.coverage.txt"
```

Report mapped percentage, properly paired percentage, duplicate rate, mean depth, and failed samples.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| BAM has no `@RG` | GATK sample tracking will fail | rerun with `-R` or use Picard `AddOrReplaceReadGroups` |
| `.bai` missing | downstream random access fails | run `samtools index` |
| very low mapping rate | wrong reference, contamination, poor reads | inspect FASTQ QC, reference version, sample identity |
| duplicate rate extreme | PCR/library issue or duplicated input lanes | inspect lane table and duplication metrics |
| contig mismatch later | mapping reference differs from calling reference | use one reference path/version across all stages |

Do not remove duplicates by default for WGS variant calling.

## 相似的 skills

- `genomics/wgs-gatk-snp-indel` - high-confidence SNP/InDel calling from these BAMs.
- `genomics/wgs-bcftools-snp-indel` - fast SNP/InDel calling from these BAMs.
- `genomics/wgs-manta-sv` - default short-read SV from these BAMs.
- `genomics/wgs-lumpy-sv` - legacy LUMPY fallback from these BAMs.
- `genomics/wgs-cnvnator-cnv` - CNVnator read-depth CNV from these BAMs.
- `genomics/rnaseq-variant-calling` - use for RNA-seq reads, not this skill.
- `genomics/long-read-sv-sniffles2` - use for ONT/PacBio reads.

## Controlled diagnostic requests

If the built-in scoped reader cannot represent an authorized object, Recovery
may select `wgs-short-read-mapping#samtools_header` or
`wgs-short-read-mapping#binary_prefix` with a declared artifact handle. Use the
canonical selected Skill ID shown in the manifest (including its category when
present). Generate must emit exactly the admitted operation's argv; code edits
require renewed admission and approval. These operations share the existing
Recovery repair budget. They produce diagnostic evidence and return to Recovery,
without satisfying the main step's output or observation contract. Input size,
output, time, access and unsupported-operation failures are distinct; none means
a file or dependency was proven absent. Larger inputs require a separately
reviewed operation contract; a prefix result is never whole-file validation.
