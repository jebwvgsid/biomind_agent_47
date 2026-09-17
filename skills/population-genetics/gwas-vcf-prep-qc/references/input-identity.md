# Input identity and row alignment (v1.1.0)

`inspect_inputs.py` reads at most 4 MiB of decoded VCF header, stopping at
`#CHROM`; small tables are limited to 16 MiB each. Its source identity covers the
VCF header only, complete phenotype/Q/map/TFAM bytes, and a GFF3 prefix only.
It does not validate all variants, associations or annotation records.
The bcftools `inspect_vcf_samples` capability publishes an observation; the
alternative `extract_vcf_samples` uses the same phase-local binding and the
existing generated-artifact mechanism when the Plan explicitly requires a
sample-list file. This separate capability selection keeps files optional for
initial inspection without changing the compiler's required-artifact semantics. The
single structured observation publishes VCF/phenotype/Q file versions and read
scope under each item's `file_identity` business field. These identities must
remain attached to the accepted observation in the next provider request;
internal parser `source` fields are not control provenance or a substitute for
that semantic delivery. The single observation can satisfy an observation-only input-check
Plan; it does not replace a nonempty sample file that the Plan explicitly
requires. Keep adverse and unknown findings in the accepted observation.

## Phenotype tables

For two-column data with no header, use `--header absent --sample-column 1
--trait 2` when sorting, or `--phenotype-header absent --sample-column 1` when
inspecting. The first row remains a sample. Auto mode recognizes conventional
ID headers (`IID`, `sample_id`, `sample`, `ID`, `FID`); unusual or ambiguous
headers require explicit options. A first sample literally called `ID` beside a
numeric/missing value is preserved as data; a numeric header requires explicit
`--header present`. Trait columns use exact header names or
positive 1-based indexes. An arbitrary trait label does not identify a column
in a table without a header. Numeric sample IDs require an explicit sample
column when the table is otherwise wholly numeric.

Duplicate/empty IDs, duplicate headers, ragged rows and invalid column choices
fail with a JSON code and diagnostic fields. TFAM `IID` is the target identity;
`FID` is not a fallback for a missing IID. Known unique IDs in a different order
are explicitly reported as `reorder_required` and sorted into TFAM order.
Missing/extra IDs fail before output creation. Missing phenotype rows may be
encoded as `NA` only under an explicit `--allow-missing true` policy. A trait
that would be entirely missing always fails. Q covariates require full sample
coverage and finite numeric values; they cannot use this missing-row escape.

## Q correspondence

A pure numeric matrix has no inferred ID column. Counts such as 215 VCF samples
and 215 Q rows establish count agreement, not row correspondence. The initial
inspection may finish with `q_mapping.status=unknown` and
`blocked_operations=[q_covariate_preparation, association_using_q]`. It must
not make a Q-dependent artifact or claim scientific completion. The sorter
rejects unknown mapping before opening output files. Do not remove Q or switch
the scientific model implicitly; any later alternative requires the existing
scientific decision and Plan revision flow.

A labeled Q table may provide actual IDs. Merely selecting `--sample-column 1`
in a wholly numeric table without a header does not establish Q correspondence;
that case still requires the explicit version-locked map below. Otherwise obtain explicit row-to-
sample assignments or independently verifiable generating provenance. For the
local helper the supplied assignments have the following strict JSON shape:

```json
{
  "schema_version": 1,
  "q_sha256": "SHA256 of the exact current Q file bytes",
  "tfam_sha256": "SHA256 of the exact target TFAM file bytes",
  "basis": "Origin and justification of these explicit row assignments",
  "rows": [
    {"q_row": 1, "sample_id": "sample_B"},
    {"q_row": 2, "sample_id": "sample_A"}
  ]
}
```

`q_row` counts physical **data** rows from 1; a recognized/explicit header is
excluded. Enumerate every data row once in order. Repeated/missing row numbers,
duplicate/missing/extra sample IDs, conflicting labeled IDs, or changed Q/TFAM
bytes are rejected. An assignment can explicitly state Q order B,A while TFAM
order is A,B; this is checked and reordered without guessing. Store the actual
basis, not a claim that matching counts proved the map. The helper verifies
consistency of supplied assignments, not independent biological provenance.
A map file path or an existing sample list alone does not satisfy this format.
Never manufacture assignments for the current task merely to pass validation.

For an unlabeled numeric Q with an established mapping:

```bash
python scripts/sort_phenotype.py --tfam target.tfam --phenotype Q.tsv \
  --header absent --input-kind q_covariates --trait 1,2,3 \
  --q-row-map supplied-row-map.json --out-emmax-covar covariates.emmax.txt
```

The map hash, current Q/TFAM hashes and alignment appear in the success
summary. All identity/column/value checks run before any output is opened;
invalid input does not replace an existing output. Output destinations must be
distinct and cannot overwrite an input/map. The helper emits no approval or
runtime authority; the parent action still uses normal binding, approval and
artifact verification.

Both executable roots explicitly delegate `scripts/input_contract.py`. Use the
selected snapshot root handle and catalog interpreter/arguments; closure
validation keeps the shared helper's version/stat/receipt checks. New formal
invocations use `--tassel-covariate true`; the direct CLI also accepts the old
zero-argument flag for compatibility.
