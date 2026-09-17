# S3 implementation handoff

Candidate: `/tmp/biomind-gwas-s1-s5-20260917/s3`.
The main repository and historical audit witnesses were not changed.

## Scope and behavior

Both GWAS Skill documents move from 1.0.0 to 1.1.0. New exact capabilities
separate bounded standard-library input inspection, verified bcftools sample
inspection, explicit sample-list extraction, phenotype preparation and Q covariate preparation. The existing
`population.genetics.gwas.vcf.prep.qc.execute` identity remains available for
existing genotype QC/conversion plans; its empty runtime list grants no claim
that a scientific tool exists. New versions/digests require the normal new
Plan acceptance; old approved objects must not be edited in place.

Two registered Python roots explicitly delegate `scripts/input_contract.py`.
Binding retains the existing interpreter/receipt/digest/stat/dependency checks.
No import scanning, shell expansion or new global schema/controller was added.

`sort_phenotype.py` validates the table and TFAM before opening any output:
explicit 1-based columns, no-header first-row preservation, duplicate/missing/
extra IDs, FID-versus-IID distinction, deterministic known-ID reordering,
missing phenotype policy and all-missing rejection. A first sample named ID
next to numeric data remains data. A wholly numeric Q cannot gain identity by
specifying `--sample-column 1` or by having the target row count.

A local explicit Q map locks current full Q and TFAM hashes and assigns every
physical data row to one unique target sample; it checks coverage, duplicate/
missing/extra IDs, row enumeration order and conflicts with file-provided IDs.
The map's basis records supplied provenance. Its positive status means those
supplied assignments are structurally consistent, not independent validation
of biological provenance. Numeric IDs in an unlabeled Q need the same explicit
map. Q values must be finite numeric values and cover all target samples.

`inspect_inputs.py` emits one existing StepEffect evidence line with
`gwas_input_structure.value`, source/read scope, sample set/order, phenotype
header/columns and Q status. Unknown Q can be a valid initial observation;
`blocked_operations` explicitly retains `q_covariate_preparation` and
`association_using_q`, and `scientific_completion=false`. The helper does not
perform association or change the approved model. S2 must transport this
accepted observation under the existing Plan dependency into the actual
provider request; S3 alone does not prove that production parent chain.

## Verification

One final unchanged local candidate ran the following in one bootstrap process:
24 new input tests, 8 new real Skill/binding/Plan-compilation tests, 18 existing root-helper tests
and 1 existing GWAS consultant authority replay: **51 passed, 19 subtests
passed**. Result: `core-final.log` and `core-final-results.json`.
The bootstrap prints `SOURCE_UNCHANGED True`; its Python-tree identity is
`98460d7f6da5293c7954db586097ac73a73ac8b25a879083d0b3e4627b7b7f8b`.
Skill/reference hashes are listed separately in `source_manifest.json`; the
Python-tree hash alone does not cover Markdown metadata.

```bash
env -i PATH=/usr/bin:/bin HOME=/tmp \
  TMPDIR=/tmp/biomind-gwas-s1-s5-20260917/s3_evidence/tmp \
  BIOMIND_DISABLE_DOTENV=1 \
  BIOMIND_OFFLINE_RESULTS=/tmp/biomind-gwas-s1-s5-20260917/s3_evidence/core-final-results.json \
  /public/home/liusiqi/anaconda3/envs/biomind/bin/python -B \
  tests/offline_managed_bootstrap.py /tmp/biomind-gwas-s1-s5-20260917/s3 -q \
  tests/test_gwas_input_contracts_s3.py tests/test_gwas_skill_contracts_s3.py \
  tests/test_unified_helper_selection.py tests/test_gwas_offline_authority_replay.py
```

Run from the candidate directory with its dedicated evidence/tmp directory
already created. Bootstrap supplies synthetic configuration and inert package-
detection startup; binding/approval/SkillManager/helper cores remain real.
Input tests call the real helper `main()` with synthetic argv and captured
streams, not a replacement parser. The two positive map cases use explicitly
constructed synthetic row truth. No package install, real model or network is
called. The first attempted bootstrap used TMPDIR=/tmp and was stopped by its
own write guard while pytest opened the directory; using the dedicated tmp
subdirectory fixed only the invocation. No guard was weakened.

The actual four formal inputs were read only by the final real helper CLI in
a clean standard-library subprocess. `formal-inputs-final.json` preserves the
exact command, stdout, return code and before/after input identities. VCF and
phenotype each have 215 samples, header absent in phenotype, first GA0001,
identical IDs/order; numeric Q is 215x3 and unknown. The VCF read covers only
its decoded header, phenotype/Q hashes cover their complete small files, GFF3
covers its first 65,536 bytes. Original source stat identities and the two
complete small-file hashes were unchanged. No scientific output was created
and no actual row map was fabricated.

The final three real Plan-effect compilation cases cover standard-library
observation-only input checks, bcftools observation-only checks, and bcftools
checks with an explicit immutable sample-list output. The latter uses
`extract_vcf_samples`, the existing generated-artifact template and the same
`runtime.bcftools.samples.bin` requirement. Removing its declared output is
rejected. Keeping `inspect_vcf_samples` separate avoids turning artifact output
into a universal inspection prerequisite: the existing compiler treats a
capability's declared artifact as required. No compiler/schema change was made.

## Compatibility and remaining integrated acceptance

The direct CLI retains the former zero-argument `--tassel-covariate`; formal
registered invocation uses an explicit true/false value, matching the existing
helper argument schema. Old unconstrained no-header trait-name guessing and
silent duplicate overwrite/all-NA success are intentionally rejected.
Known labeled/numeric-column valid inputs retain the same output layouts.
The existing formal empty required sample-artifact validator is unchanged;
S3→S5 should assert its rejection alongside rc0 and exact validation failure.
The S1/S2 automatic discovery→inspection→ledger→request→approval→runner chain
must be tested on the parent's final cumulative source. These local test counts
must not be added to another candidate's counts as a joint acceptance proof.

Actual Q provenance, scientific GWAS, real providers, installation and host
inspection facilities remain outside this slice's evidence. The current
source's previous runtime/approval/binding/CLI safeguards are retained.
