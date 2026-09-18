---
name: gwas-vcf-prep-qc
description: Prepare resequencing VCF, phenotype, Q matrix, PCA covariates, and PLINK/EMMAX/TASSEL input files for GWAS. Use before SNP, InDel, or SV association testing when BioMind must filter markers, convert VCF to PLINK transposed files, sort phenotypes to genotype sample order, encode qualitative traits, prepare TASSEL files, or validate sample and chromosome compatibility.
version: "1.1.0"
tags: [GWAS, VCF, PLINK, EMMAX, TASSEL, phenotype, covariates, PCA, Q-matrix, QC]
legacy_ids: [gwas-prep-qc, gwas-vcf-qc, emmax-tassel-prep, GWAS输入准备]
task_types: [gwas_population_genetics]
stage_tags: [gwas_input_prep, genotype_qc, phenotype_alignment]
inputs: [vcf, phenotype_table, trait, q_matrix, pca, marker_type]
outputs: [tped, tfam, emmax_phenotype, tassel_trait, covariate_files, qc_summary]
runtime:
  default_env: popgen_gwas
  env_runner: bash
  language: bash
  packages: [bcftools, plink, plink2, beagle, openjdk, python]
  capabilities: [vcf-qc, plink-conversion, phenotype-sorting, covariate-prep]
  healthcheck: bash -lc "bcftools --version >/dev/null && (plink --version >/dev/null || plink2 --version >/dev/null) && python skills/population-genetics/gwas-vcf-prep-qc/scripts/sort_phenotype.py --help >/dev/null"
  inspection_probes:
  - output: RESOLVED_BCFTOOLS_SAMPLES_BIN
    value: "{prefix}/bin/bcftools"
    checks:
    - argv: ["{prefix}/bin/bcftools", "--version"]
      success_codes: [0]
      contains: [bcftools]
    - argv: ["{prefix}/bin/bcftools", "query", "--help"]
      success_codes: [0, 1]
      contains: ["Usage:", query]
generate:
  script_invocations:
    scripts/inspect_inputs.py:
      schema_version: 1
      runtime: controller_python
      delegates: [scripts/input_contract.py]
      arguments:
        --vcf: {nargs: 1, required: true}
        --phenotype: {nargs: 1, required: true}
        --q-matrix: {nargs: 1}
        --gff3: {nargs: 1}
        --phenotype-header: {nargs: 1}
        --sample-column: {nargs: 1}
        --q-row-map: {nargs: 1}
        --tfam: {nargs: 1}
        --observation-id: {nargs: 1, required: true}
    scripts/sort_phenotype.py:
      schema_version: 1
      runtime: controller_python
      delegates: [scripts/input_contract.py]
      arguments:
        --tfam: {nargs: 1, required: true}
        --phenotype: {nargs: 1, required: true}
        --trait: {nargs: 1, required: true}
        --header: {nargs: 1}
        --sample-column: {nargs: 1}
        --input-kind: {nargs: 1}
        --q-row-map: {nargs: 1}
        --allow-missing: {nargs: 1}
        --out-emmax: {nargs: 1}
        --out-emmax-covar: {nargs: 1}
        --out-tassel: {nargs: 1}
        --tassel-covariate: {nargs: 1}
  execution_capabilities:
  - capability_id: population.genetics.gwas.vcf.prep.qc.inspect_inputs
    operation: inspect_inputs
    contract_revision: gwas-vcf-prep-qc-v1.1.0
    required_runtime_binding_ids: []
    optional_runtime_binding_ids: []
    required_artifact_binding_ids: []
    produces_artifacts: []
    produces_bindings: []
    produces_observations:
    - observation_id: gwas_input_structure
      evidence_contract: {required_fields: [value]}
    workspace_mutations: []
    explicit_side_effects: []
    execution_guidance:
    - Run the selected scripts/inspect_inputs.py with explicit VCF, phenotype, optional Q/GFF3, and observation-id gwas_input_structure. It uses controller Python and bounded standard-library input reads; this inspection requires no scientific runtime binding or output file.
    - Declare outputs=[] for this observation-only initial check. The workflow's recommended 00.input_check layout does not declare an output, and this helper does not create that directory. Do not create an empty directory to satisfy a Plan. If an already approved Plan requires it, request a Plan revision and approval, or use a separately declared real file-producing action; never delete the requirement after execution.
    - Use the published direct argv or supported simple shell invocation. Do not wrap this fixed helper in if/then/fi or add existence-test compound shell; helper validation reports actual missing input files. This restriction belongs to this helper invocation contract, not all scientific shell scripts.
    - Read references/input-identity.md before interpreting the observation. Preserve source hashes and read scopes, phenotype header/columns and alignment, and Q mapping status. A header/prefix inspection does not validate the complete VCF/GFF3.
    - A numeric Q with no sample IDs remains unknown even when all row counts equal 215. Report a valid initial observation, but block Q covariate preparation and association_using_q until explicit row correspondence is version-checked. Do not create a map from equal counts or silently omit Q.
    - Keep the task-level report.md, reproduce.sh and stage_status.tsv deliverables as explicit later Plan results. An observation-only step does not remove final reporting obligations.
  - capability_id: population.genetics.gwas.vcf.prep.qc.inspect_vcf_samples
    operation: inspect_vcf_samples
    contract_revision: gwas-vcf-prep-qc-v1.1.0
    required_runtime_binding_ids: [runtime.bcftools.samples.bin]
    optional_runtime_binding_ids: []
    required_artifact_binding_ids: []
    produces_artifacts: []
    produces_bindings: []
    produces_observations:
    - observation_id: gwas_input_structure
      evidence_contract: {required_fields: [value]}
    workspace_mutations: []
    explicit_side_effects: []
    execution_guidance:
    - Use this capability only for bcftools VCF sample/header inspection. Resolve runtime.bcftools.samples.bin for this phase and execute the verified bound executable with query -l and the explicit input VCF. Missing downstream EMMAX/R/PLINK does not invalidate this local capability.
    - Publish a scoped gwas_input_structure observation with sample IDs/count, actual input identity and inspection scope. If the Plan instead requires a nonempty sample file, choose extract_vcf_samples and keep that formal file validation and never publish an empty required sample file as successful.
    - An available bcftools candidate is not workflow readiness. Preserve unknown phenotype/Q correspondence until separately inspected; see references/input-identity.md. Never infer Q row order from the number of VCF samples.
  - capability_id: population.genetics.gwas.vcf.prep.qc.extract_vcf_samples
    operation: extract_vcf_samples
    contract_revision: gwas-vcf-prep-qc-v1.1.0
    required_runtime_binding_ids: [runtime.bcftools.samples.bin]
    optional_runtime_binding_ids: []
    required_artifact_binding_ids: []
    produces_artifacts: [{artifact_id: generated_artifact, resource_kind: immutable_artifact}]
    produces_bindings: []
    produces_observations: []
    workspace_mutations: []
    explicit_side_effects: []
    execution_guidance:
    - Choose this file-producing alternative only when the current Plan explicitly requires an immutable sample-list output. Bind that output through step.outputs and use the phase-verified runtime.bcftools.samples.bin executable for query -l on the exact input VCF.
    - A required sample list must be nonempty and pass the existing formal artifact validator. A failed or unavailable query must not be replaced with an empty file or a success claim. This local result does not establish phenotype/Q correspondence or downstream tool readiness.
    - For an observation-only Plan choose inspect_vcf_samples instead. Initial inspection does not universally require a file; see references/input-identity.md.
  - capability_id: population.genetics.gwas.vcf.prep.qc.prepare_phenotype
    operation: prepare_phenotype
    contract_revision: gwas-vcf-prep-qc-v1.1.0
    required_runtime_binding_ids: []
    optional_runtime_binding_ids: []
    required_artifact_binding_ids: []
    produces_artifacts: [{artifact_id: generated_artifact, resource_kind: immutable_artifact}]
    produces_bindings: []
    produces_observations: []
    workspace_mutations: []
    explicit_side_effects: []
    execution_guidance:
    - Use selected scripts/sort_phenotype.py with the target TFAM and explicit trait columns. No-header two-column input uses --header absent --sample-column 1 --trait 2; never consume its first sample as a header. Read references/input-identity.md for duplicate/missing/order handling.
  - capability_id: population.genetics.gwas.vcf.prep.qc.prepare_q_covariates
    operation: prepare_q_covariates
    contract_revision: gwas-vcf-prep-qc-v1.1.0
    required_runtime_binding_ids: []
    optional_runtime_binding_ids: []
    required_artifact_binding_ids: []
    produces_artifacts: [{artifact_id: generated_artifact, resource_kind: immutable_artifact}]
    produces_bindings: []
    produces_observations: []
    workspace_mutations: []
    explicit_side_effects: []
    execution_guidance:
    - Before any Q-dependent output, read references/input-identity.md and require observed Q IDs or a supplied explicit row map tied to current Q and TFAM hashes. Use scripts/sort_phenotype.py --input-kind q_covariates with explicit numeric/header column selection and --q-row-map only when that mapping actually exists and is justified.
    - Unknown Q mapping forbids Q covariate preparation and association_using_q. Initial inspection may still succeed with an unknown observation. A path, row count, guessed order or newly fabricated map is not mapping evidence. Do not silently discard Q or switch the approved model.
  - capability_id: population.genetics.gwas.vcf.prep.qc.execute
    operation: execute
    contract_revision: gwas-vcf-prep-qc-v1.1.0
    required_runtime_binding_ids: []
    optional_runtime_binding_ids: []
    required_artifact_binding_ids: []
    produces_artifacts: [{artifact_id: generated_artifact, resource_kind: immutable_artifact}]
    produces_bindings: []
    produces_observations: []
    workspace_mutations: []
    explicit_side_effects: []
    execution_guidance:
    - Compatibility capability for existing explicit genotype QC/conversion plans; its empty runtime list does not certify any tool availability. Preserve actual executable and approval checks. New sample inspection should choose inspect_inputs or inspect_vcf_samples for the actual operation.
    - Before phenotype/Q preparation read references/input-identity.md and use the appropriate helper capability. Unknown sample mapping must not be bypassed by this legacy capability; do not infer Q order or silently change the approved scientific model.
---

# GWAS VCF Prep QC

Prepare genotype, phenotype, and covariate files before EMMAX or TASSEL association testing.

## 确认任务边界

Use this skill when:

- Input is SNP/InDel/SV VCF and a phenotype table.
- The next stage is EMMAX, TASSEL GLM, TASSEL MLM, or Manhattan plotting.
- BioMind must align sample order across genotype, phenotype, and covariates.
- Qualitative phenotypes need explicit numeric recoding.

Do not use this skill for crop-specific PLINK2/VCFtools wrappers when the user requests that route; use `crop-breeding/crop-gwas-qc-plink2-vcftools`.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `VCF` | marker VCF, bgzipped and indexed when possible |
| `PHENOTYPE` | table with sample ID and trait |
| `TRAIT` | target trait column |
| `OUTDIR` | output root |

Optional:

| Parameter | Default / rule |
|---|---|
| `MARKER_TYPE` | `snp`; use `indel` or `sv` for relaxed defaults |
| `MAF` | SNP `0.05`, InDel/SV `0.01` |
| `GENO` | SNP `0.1`, InDel/SV `0.5` |
| `Q_MATRIX` | ADMIXTURE or structure covariates |
| `N_PCS` | `3` when PCA is requested |
| `RECODE_MAP` | required for categorical traits |
| `IMPUTE` | off unless requested or justified by missingness |

Preflight is a separate, bounded input-check phase. Read
`references/input-identity.md` before choosing columns or interpreting Q order.
The standard-library helper can publish `gwas_input_structure` without a
scientific environment binding or durable report file:

```bash
python skills/population-genetics/gwas-vcf-prep-qc/scripts/inspect_inputs.py \
  --vcf "${VCF}" --phenotype "${PHENOTYPE}" --q-matrix "${Q_MATRIX}" \
  --observation-id gwas_input_structure
```

Omit `--q-matrix` when no Q was supplied; an empty path is not a valid Q input.
The formal helper action uses the selected snapshot's catalog interpreter/path
and root script handle, not an invented interpreter path. The explicit delegate
is included by the existing binding closure. If the Plan explicitly uses
bcftools for a scoped observation, select `inspect_vcf_samples`; if it explicitly
requires a sample-list file, select `extract_vcf_samples`. Both use the phase-local
verified `runtime.bcftools.samples.bin`; the absence of downstream association
or plotting tools does not invalidate that inspection. Do not pre-create an
empty required sample file or substitute a successful version probe for sample
extraction. Required nonempty artifact validation remains mandatory.

Only a later approved preparation step creates its declared output directories.

Convert VCF to PLINK transposed format for EMMAX:

```bash
plink \
  --vcf "${VCF}" \
  --maf "${MAF:-0.05}" \
  --geno "${GENO:-0.1}" \
  --recode 12 transpose \
  --output-missing-genotype 0 \
  --out "${OUTDIR}/01.prep/markers" \
  --set-missing-var-ids @:# \
  --allow-extra-chr \
  --keep-allele-order \
  > "${OUTDIR}/logs/plink.recode.log" 2>&1
```

Sort phenotype and covariates:

```bash
python skills/population-genetics/gwas-vcf-prep-qc/scripts/sort_phenotype.py \
  --tfam "${OUTDIR}/01.prep/markers.tfam" \
  --phenotype "${PHENOTYPE}" \
  --trait "${TRAIT}" \
  --out-emmax "${OUTDIR}/01.prep/trait.emmax.txt" \
  --out-tassel "${OUTDIR}/01.prep/trait.tassel.txt"
```

For Q/PCA covariates with an explicit sample column, use the helper below. For
an unlabeled numeric matrix, first obtain a real explicit row assignment, then
use `--q-row-map` and numeric column indexes as documented in
`references/input-identity.md`. Equal counts and a sample-list path do not
establish correspondence. Until resolved, stop Q-dependent preparation and
association; the initial inspection remains a valid result.

```bash
python skills/population-genetics/gwas-vcf-prep-qc/scripts/sort_phenotype.py \
  --tfam "${OUTDIR}/01.prep/markers.tfam" \
  --phenotype qmatrix.tsv \
  --input-kind q_covariates \
  --trait Q1,Q2,Q3 \
  --out-tassel "${OUTDIR}/01.prep/qmatrix.tassel.txt" \
  --out-emmax-covar "${OUTDIR}/01.prep/qmatrix.emmax.txt" \
  --tassel-covariate true
```

## 输出结果

Expected outputs:

- `01.prep/markers.tped`
- `01.prep/markers.tfam`
- `01.prep/trait.emmax.txt`
- `01.prep/trait.tassel.txt`
- optional `01.prep/qmatrix.emmax.txt`
- optional `01.prep/qmatrix.tassel.txt`
- optional `01.prep/pca.eigenvec`
- `00.input_check/vcf.samples.txt`
- `logs/plink.recode.log`

Validation:

```bash
test -s "${OUTDIR}/01.prep/markers.tped"
test -s "${OUTDIR}/01.prep/markers.tfam"
awk 'END{print NR}' "${OUTDIR}/01.prep/markers.tfam"
awk 'END{print NR}' "${OUTDIR}/01.prep/trait.emmax.txt"
```

Phenotype IDs must match genotype IIDs. Wrong order is explicitly reported and
reordered only when identities match. Missing rows fail by default; only an
explicit `--allow-missing true` phenotype policy can emit missing values, and an
all-missing trait always fails. Extra/duplicate IDs, ambiguous columns and
numeric Q without identity are rejected before output creation. No-header input
requires numeric trait indexes (for example `--header absent --trait 2`).

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| sample IDs unmatched | genotype/phenotype not aligned | normalize IDs or create a sample map |
| categorical trait has text values | association tools need numeric phenotype | require explicit recoding map |
| PLINK duplicate marker IDs | marker names not stable | use `--set-missing-var-ids @:#` or documented marker IDs |
| Q matrix lacks sample column | row-to-sample mapping is unknown | obtain explicit row assignments or verifiable generating provenance, validate file versions and IDs; never paste a guessed order |
| imputation requested blindly | may alter marker distribution | run only when requested or justified |

Do not treat missing phenotype values as zero.

## 相似的 skills

- `population-genetics/gwas-emmax-association` - EMMAX after this prep.
- `population-genetics/gwas-tassel-glm-mlm` - TASSEL after this prep.
- `population-genetics/gwas-manhattan-qq-plot` - plot standardized association results.
- `crop-breeding/crop-gwas-qc-plink2-vcftools` - crop-specific PLINK2 QC route.
