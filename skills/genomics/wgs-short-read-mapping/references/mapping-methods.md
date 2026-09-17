# Mapping methods and examples (read for the current phase)
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

These four inspection helpers are **Python source files**, not standalone
executables. Their `generate.script_invocations` contracts select
`controller_python`: the same Python installation that runs BioMind and provides
its `agent_core`/`execution` modules. This utility runtime is separate from the
`bio_wgs` environment used by scientific alignment tools.

The resource catalog supplies a complete, shell-quoted `invocation.shell_prefix`
(the bound Python interpreter followed by the bound script path), as well as its
equivalent `argv_prefix`. Copy that exact prefix and append the declared option
tokens. For the mixed-input helper, the command shape is:

```text
<published invocation.shell_prefix> \
  --observation-id <current declared observation identity> \
  --evidence-dir "$BIOMIND_PREFLIGHT_EVIDENCE_DIR" \
  --fastq <sample mate label> <quoted bound FASTQ path> \
  --reference <reference label> <quoted bound FASTA path>
```

Replace the angle-bracket descriptions with current supplied values; repeat
`--fastq` for each file. Each label/path is a separate argument. A quoted
`BIOMIND_SKILL_*` script-path variable may replace the second argv token, but
cannot replace the interpreter token. Do not run the source path alone, use a
bare `python` from ambient PATH, or chmod the Skill file.

The single-file readers support direct argv or simple shell commands. The batch
and mixed-input helpers require the shell form because their evidence-directory
variable is resolved only after approval. Simple shell permits optional
`set -euo pipefail` and `exec`, comments, quoting and line continuations. Keep the
helper invocation direct; dynamic wrappers, assignments, heredocs and compound
shell require a different explicitly supported contract. Delegated resources
must be declared and pinned, but need not be separately invoked. Invocation
validation happens before approval; it checks entrypoint/argument shape, not
input validity or scientific success. No approved script is rewritten on retry.

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

Unknown ploidy need not block these mapping/input checks. Carry it as an
unresolved user-owned decision to the calling Skill and the same Plan's
ask_user dependency; a mapping receipt or general Plan approval cannot confirm
ploidy or validate downstream genotype filters.

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
| `ALIGNER` | explicitly selected, verified compatible implementation | keep its binding and command in the approved action |
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
