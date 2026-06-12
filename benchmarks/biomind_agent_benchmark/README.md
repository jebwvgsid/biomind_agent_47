# BioMind Agent Benchmark

This folder is the single home for BioMind benchmark questions, expected answers, data-source notes, known issues, and test-data scripts. Skill folders should contain reusable domain workflows and analysis runners only; they should not own benchmark data generation.

## Layout

- `questions/`: benchmark prompts and MCQ routing questions.
- `answers/`: BioMed-AQA-style expected-answer keys used by humans or evaluators.
- `errors/`: known environment, dependency, and benchmark-data issues.
- `sources/`: public data sources, simulator policy, and BioMed-AQA alignment notes.
- `scripts/generate/`: active professional simulation scripts. Current active simulator: `msprime`.
- `scripts/download/`: active public database/demo data downloaders.
- `scripts/validate/`: benchmark and public-data validators.
- `scripts/run/`: runnable smoke tests using active professional or downloaded data.
- `scripts/legacy/`: deprecated toy generators kept for traceability, not active benchmark data.
- `manifests/`: generated fixture manifests.
- `fixtures_professional/`: professional simulator outputs when generated under the benchmark folder.
- `fixtures_public/`: downloaded public-data subsets when generated under the benchmark folder.

## Active Data Policy

Active benchmark data should come from one of two sources:

1. Professional simulation: `msprime` coalescent ancestry and mutation simulation.
2. Public database/demo data: downloaded and subsetted with source URLs, checksums, and `answer_key.yaml`.

Hand-written NumPy/Pandas toy data are no longer the active benchmark source. They may remain under `scripts/legacy/` only to explain older results.

## Question And Answer Format

The benchmark follows the BioMed-AQA shape: each open-ended task has a natural
language prompt, reference steps, milestone-style scoring, input files or input
requirements, and expected artifacts. The companion answer key is
`answers/expected_answers.yaml`; it contains one `open_task_answers` entry for
each task and one `mcq_answers` entry for each MCQ.

The source mapping is documented in `sources/biomed_aqa_alignment.md`. BioMind
uses the structure of BioMed-AQA, but the questions, skills, fixtures, and
failure rules are rewritten for this local project.

In v0.3, active open tasks are data-backed result-choice tasks. Each prompt must
name the fixture paths directly, and scoring includes path validation, primary
skill selection, analysis or honest blocking, metric extraction, and final
answer selection. `data_files.availability` is `available` for files present in
the current workspace and `planned` for server-side or later-copied fixtures.

## Generate msprime Data

```bash
python benchmarks/biomind_agent_benchmark/scripts/generate/generate_msprime_fixtures.py \
  --outdir results/linux_professional_synthetic \
  --seed 20260529
```

This creates:

- `msprime_variants.vcf`
- `haplotypes.fasta`
- `genotype_matrix.tsv`
- `phenotype.tsv`
- `gwas_summary.tsv`
- `marker_annotation.tsv`
- `marker_effects.tsv`
- `sample_groups.tsv`
- `simulation_truth.tsv`

The genotype/VCF/haplotype data come from `msprime`. The phenotype and GWAS-like p-values are BioMind-controlled truth labels derived from the simulated genotypes.

## Run Crop msprime Smoke

```bash
python benchmarks/biomind_agent_benchmark/scripts/run/run_msprime_crop_gp_smoke.py \
  --fixture-dir results/linux_professional_synthetic \
  --outdir results/linux_msprime_crop_gp_smoke \
  --seed 20260529
```

This runs crop genomic prediction and GP-WAITER-style weighted prediction from the msprime fixture. It does not use toy synthetic multi-omics/GxE/SV fixtures.

## Download Public Demo Fixtures

```bash
python3 benchmarks/biomind_agent_benchmark/scripts/download/download_public_demo_fixtures.py \
  --outdir benchmarks/biomind_agent_benchmark/fixtures_public \
  --rawdir .biomind/public_raw

python3 benchmarks/biomind_agent_benchmark/scripts/validate/validate_public_demo_fixtures.py
```

The downloader records source URLs and writes `answer_key.yaml` files beside downloaded subsets.

## Validate Benchmark Metadata

```bash
python benchmarks/biomind_agent_benchmark/scripts/validate/validate_benchmark.py
```

The validator checks question structure, MCQ answers, local skill references, and active fixture manifests.
Planned fixture entries are validated for metadata shape but are not required to
exist until they are promoted to `available`.
