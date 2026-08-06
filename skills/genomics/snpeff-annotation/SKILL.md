---
name: snpeff-annotation
description: Build or use snpEff databases and annotate VCF variants with ANN effects. Use when the user asks for SNP/indel functional annotation with snpEff, including existing pre-built databases, custom GFF3/GTF/GenBank database builds, ANN interpretation, summary HTML, and safe database/version selection. Do not guess species databases, genome-version names, chromosome names, codon tables, or unchecked-build options from taxonomy labels alone.
metadata:
  renderer:
    renderer_id: genomics.snpeff_annotation
    entrypoint: scripts/render_snpeff_annotation_scripts.py
    runner: controller_python
    manifest_filename: manifest.json
    manifest_type: renderer_artifact_contract
    schema_version: 3
    composite_step: true
    owned_outputs:
      - annotated.vcf
      - snpEff_summary.html
      - ann_effect_summary.tsv
      - ann_gene_summary.tsv
      - report.md
      - reproduce.sh
      - stage_status.tsv
    invocation:
      arguments:
        vcf: {flag: --vcf, required: true, aliases: [variant_file]}
        genome_version: {flag: --genome-version, required: true}
        outdir: {flag: --outdir, aliases: [output_dir], top_level: true}
        output_vcf: {flag: --output-vcf}
        snpeff_jar: {flag: --snpeff-jar, runtime_binding: SNPEFF_JAR}
        snpeff_command: {flag: --snpeff-command}
        java_options: {flag: --java-options}
        config: {flag: --config}
        data_dir: {flag: --data-dir}
        reference_fasta: {flag: --reference-fasta}
        annotation_gff3: {flag: --annotation-gff3}
        annotation_gtf: {flag: --annotation-gtf}
        annotation_genbank: {flag: --annotation-genbank}
        genome_name: {flag: --genome-name}
        cds_fasta: {flag: --cds-fasta}
        protein_fasta: {flag: --protein-fasta}
        config_line: {flag: --config-line, repeatable: true}
        allow_unchecked_build: {flag: --allow-unchecked-build, boolean: true}
        compress_output: {flag: --compress-output, boolean: true}
      routed_arguments:
        annotation_file:
          selector: annotation_format
          routes:
            "-gtf22": annotation_gtf
            gtf22: annotation_gtf
            "-gff3": annotation_gff3
            gff3: annotation_gff3
            "-genbank": annotation_genbank
            genbank: annotation_genbank
      runtime_only_arguments: [java_home]
      controller_owned_arguments: [producer_step_id]
  runtime:
    default_env: bio_gatk
    env_runner: bash
    language: bash
    tools: [snpEff, java]
    packages: [snpeff, openjdk, gatk4, samtools, bcftools]
    capabilities: [snpeff-annotation, snpeff-custom-db, ann-effect-summary]
    install_tool: genomics_snpeff_annotation_runtime
    healthcheck: >-
      bash -lc 'SNPEFF_JAVA_HOME="${SNPEFF_JAVA_HOME:-}"; if [ -z "$SNPEFF_JAVA_HOME" ] &&
      [ -n "${BIO_SOURCE_INSTALL_DIR:-}" ] && [ -x "${BIO_SOURCE_INSTALL_DIR}/temurin-21/bin/java" ]; then
      SNPEFF_JAVA_HOME="${BIO_SOURCE_INSTALL_DIR}/temurin-21"; fi;
      SNPEFF_JAR=$(find "$CONDA_PREFIX/share" -maxdepth 4 -type f -name snpEff.jar -print -quit);
      test -s "$SNPEFF_JAR" || exit 1; if [ -n "$SNPEFF_JAVA_HOME" ]; then
      test -x "$SNPEFF_JAVA_HOME/bin/java" || exit 1;
      "$SNPEFF_JAVA_HOME/bin/java" -jar "$SNPEFF_JAR" -version >/dev/null || exit 1;
      else command -v snpEff >/dev/null || exit 1; snpEff -version >/dev/null || exit 1;
      java -version >/dev/null || exit 1;
      SNPEFF_JAVA_HOME=$(dirname "$(dirname "$(command -v java)")"); fi;
      printf "RESOLVED_ENV_PREFIX=%s\nRESOLVED_SNPEFF_JAR=%s\nRESOLVED_JAVA_HOME=%s\n"
      "$CONDA_PREFIX" "$SNPEFF_JAR" "$SNPEFF_JAVA_HOME"'
    install_policy: prefer-declared
    install_command: conda install -n bio_gatk -c conda-forge -c bioconda -y snpeff openjdk
---

# snpEff Annotation

Use this skill for VCF functional annotation with snpEff.

Aliases and trigger phrases: snpEff, SnpEff, ANN, variant effect annotation, functional annotation, build snpEff database, `genes.gff`, `genes.gtf`, `snpEff.config`.

## Core Rules

- Treat `genome-version` as the snpEff database key. It must match the `snpEff.config` entry and the data directory name, for example `data/<genome-version>/`.
- Do not infer `genome-version` from species, taxonomy, chromosome count, chromosome prefix, or a previous project.
- Prefer an official pre-built snpEff database when the exact reference build and annotation release match the user's VCF.
- Build a custom database only when the user provides the reference FASTA plus a matching annotation source such as GTF, GFF3, or GenBank.
- Require chromosome/contig names to match between VCF, FASTA, and annotation. Inspect headers before annotation when uncertain.
- Use paths that are visible in the shell environment where the rendered Bash scripts will run; do not pass local desktop paths to cluster scripts unless they are mounted there.
- Prefer GTF 2.2 for large eukaryotic genomes when available. Use GFF3 when that is the provided authoritative annotation. Use GenBank mainly for small genomes or when GenBank is the authoritative source.
- Treat CDS/protein consistency files as `optional_validation` for custom GTF/GFF builds. When absent, skip only the corresponding validation, record `skipped_optional_missing`, and continue; this is not an unchecked build because no required validation was skipped.
- Do not hard-code codon tables. If organellar, bacterial, plastid, mitochondrial, or other non-standard codon usage may apply, ask for or inspect the required snpEff config lines.

## Official Documentation Anchors

- Running and annotation command pattern: `https://pcingola.github.io/SnpEff/snpeff/running/`
- Input/output and ANN field: `https://pcingola.github.io/SnpEff/snpeff/inputoutput/`
- Database building: `https://pcingola.github.io/SnpEff/snpeff/build_db/`

## Runtime and Dependency Discovery

- Reuse `bio_gatk` for this skill. It is the shared short-read variant environment and should contain GATK/Picard plus `snpEff`; do not create a one-off `bio_snpeff` environment by default.
- Verify the declared runtime first. The skill uses `${BIO_SOURCE_INSTALL_DIR}/temurin-21` when that executable exists; it verifies the packaged `snpEff.jar` directly in the bounded `$CONDA_PREFIX/share` area. This avoids treating a broken Java launcher wrapper as proof that the installed snpEff package is absent.
- Never construct a Conda prefix by appending `anaconda3` or an environment name to the project/benchmark root. Resolve the JAR inside the verified environment, for example `SNPEFF_JAR=$(conda run -n bio_gatk bash -c 'find "$CONDA_PREFIX/share" -maxdepth 4 -type f -name snpEff.jar -print -quit')`, verify it is non-empty, and then pass that exact path to the renderer.
- `SNPEFF_JAVA_HOME` may override that location for one command/session, but it is not a required `.env` setting. Do not globally replace the Java used by unrelated tools.
- If the command wrapper is absent but the environment exists, search only the environment package area with a depth bound: `conda run -n bio_gatk bash -c 'find "$CONDA_PREFIX/share" -maxdepth 4 -name snpEff.jar -type f -print -quit'`.
- Never recursively scan `/`, a whole home directory, or a benchmark tree to discover `snpEff.jar` or `snpEff.config`. Never download a “latest” archive as a discovery fallback.
- If the declared check fails, delegate exactly once through `<install_tool env="bio_gatk">genomics_snpeff_annotation_runtime</install_tool>`. The installer must use the declared package contract, re-run the healthcheck, and then register the same environment. If installation is forbidden or still fails, write a truthful stage-blocked status instead of searching elsewhere.

## Reusable Renderer

Render scripts for annotating with an existing database. Resolve the JAR inside the verified `bio_gatk` environment first; the renderer itself is a standard-library Python utility and should use the BioMind/current Python rather than `bio_gatk`'s Python:

```bash
SNPEFF_JAR=$(conda run -n bio_gatk bash -c 'find "$CONDA_PREFIX/share" -maxdepth 4 -type f -name snpEff.jar -print -quit')
test -s "$SNPEFF_JAR"
python skills/genomics/snpeff-annotation/scripts/render_snpeff_annotation_scripts.py \
  --vcf variants.vcf.gz \
  --genome-version project_or_official_database_key \
  --snpeff-jar "$SNPEFF_JAR" \
  --config /abs/path/snpEff.config \
  --outdir snpeff_annotation
```

Render scripts for building a custom GFF3 database and then annotating:

```bash
SNPEFF_JAR=$(conda run -n bio_gatk bash -c 'find "$CONDA_PREFIX/share" -maxdepth 4 -type f -name snpEff.jar -print -quit')
test -s "$SNPEFF_JAR"
python skills/genomics/snpeff-annotation/scripts/render_snpeff_annotation_scripts.py \
  --vcf variants.vcf.gz \
  --genome-version project_reference_release \
  --snpeff-jar "$SNPEFF_JAR" \
  --reference-fasta /abs/path/reference.fa \
  --annotation-gff3 /abs/path/annotation.gff3 \
  --outdir snpeff_annotation
```

For a custom GTF 2.2 database, use the renderer's real GTF interface. The
renderer adds `build -gtf22` to `02.build_database.sh`; `--gtf22`, `--gtf`,
`--fasta`, and `--output-dir` are not renderer CLI flags:

```bash
SNPEFF_JAR=$(conda run -n bio_gatk bash -c 'find "$CONDA_PREFIX/share" -maxdepth 4 -type f -name snpEff.jar -print -quit')
test -s "$SNPEFF_JAR"
/path/to/current/biomind/python skills/genomics/snpeff-annotation/scripts/render_snpeff_annotation_scripts.py \
  --vcf variants.vcf \
  --reference-fasta reference.fa \
  --annotation-gtf annotation.gtf \
  --genome-version project_reference_release \
  --snpeff-jar "$SNPEFF_JAR" \
  --outdir snpeff_annotation
```

Use the `controller_python` reported in the runtime context for
`/path/to/current/biomind/python`. Do not run this standard-library renderer
with `conda run -n bio_gatk python`; `bio_gatk` supplies snpEff and Java, not
the renderer interpreter.

Run each rendered script with the declared Bash runner, in manifest order. Do not derive or guess an environment prefix from the project root.

The renderer writes scripts in manifest order: `00.preflight.sh`,
`01.prepare_custom_database.sh` when custom inputs are provided,
`02.build_database.sh` for custom builds or `02.download_database.sh` for an
existing database, `03.annotate_vcf.sh`, `04.summarize_ann.sh`, and
`05.finalize.sh`. The final two scripts create the ANN effect summary and the
declared status/report/reproduction artifacts; Planner must consume these
manifest paths instead of inventing equivalent filenames.

The renderer's `manifest.json` is the authoritative physical artifact contract. Planner should request logical outputs such as `snpeff_database_build_script`; it must not invent alternatives such as `build_db.sh`. After rendering, use each manifest artifact's `artifact_id`, `path`, `producer_step_id`, and `validator`. If older plan text names a different file, reconcile the plan state to the manifest and keep the old value only as audit history; do not copy the script or rerun a successful renderer.

`02.download_database.sh` requires network access and should be run only when the user has confirmed a matching official snpEff database. For offline clusters or custom references, build or point to a local database instead.

## Existing Database Workflow

1. Confirm that the VCF reference build equals the snpEff database build. Do not use a database just because species names match.
2. List or download databases only when network access is allowed:
   `java -jar snpEff.jar databases` or `java -jar snpEff.jar download -v <genome-version>`.
3. Annotate:
   `java -Xmx8g -jar snpEff.jar -v <genome-version> input.vcf.gz > output.snpeff.ann.vcf`.
4. Check output:
   - VCF header contains `##SnpEffVersion` and `##INFO=<ID=ANN`.
   - `snpEff_summary.html` and `snpEff_genes.txt` are produced when enabled.
   - Variants have plausible ANN fields and no systematic chromosome-not-found warnings.

## Custom Database Workflow

1. Choose a project-specific `genome-version` from the exact reference and annotation release, such as `species_ref_release`, not a broad species or taxonomy label.
2. Create a project config with:
   `<genome-version>.genome : <human-readable name>`
   plus any required codon table lines.
3. Place files in snpEff's expected layout:
   - GTF: `data/<genome-version>/genes.gtf` or `.gz`; reference FASTA as `data/genomes/<genome-version>.fa` or `data/<genome-version>/sequences.fa`.
   - GFF3: `data/<genome-version>/genes.gff` or `.gz`; reference FASTA as above unless embedded after `##FASTA`.
   - GenBank: `data/<genome-version>/genes.gbk`; concatenate multiple GenBank records only when they belong to the same reference build.
4. Build with the matching format flag: `build -gtf22`, `build -gff3`, or `build -genbank`.
5. Provide `cds.fa` and/or `protein.fa` when available. Their requirement level is `optional_validation`: when absent, the renderer disables only the unavailable check (`-noCheckCds`, `-noCheckProtein`, or both), records `optional_validation_skipped`, the reason, and scientific limitation in `manifest.json`, `report.md`, `stage_status.tsv`, and `reproduce.sh`, then continues. Do not ask repeatedly or promote these optional files to required. Only skipping a contract-declared required validator may be called unchecked.
6. The build subcommand must immediately follow the snpEff launcher/JAR prefix, before build-specific flags such as `-gtf22`; an `Unknown option` message that reports `Command: 'ann'` is evidence to inspect launcher/subcommand ordering, not to remove `-gtf22`.
7. Annotate the VCF only after the database build succeeds. Treat a config,
   database directory, copied `genes.gtf`/`sequences.fa`, and build log as
   partial artifacts only. The build acceptance check must verify the declared
   core predictor file `data/<genome-version>/snpEffectPredictor.bin` exists and
   is non-empty, the final build-log section has no fatal result such as
   `Database check failed`, and the database can be loaded by the same launcher
   during the annotation preflight. If any condition fails, keep the database
   stage failed/partial and do not run annotation.

Read `references/snpeff_config_templates.md` before creating a custom database config or adding any `--config-line`. It gives the supported layouts, a minimal project config, and the cases that require user confirmation.

## ANN Interpretation

- snpEff writes functional annotations to the VCF INFO `ANN` field by default.
- A single variant may have multiple ANN records because it can affect multiple transcripts, genes, or ALT alleles.
- Key ANN subfields to report: allele, effect, impact, gene name, gene ID, feature type, transcript/feature ID, biotype, exon/intron rank, HGVS.c, HGVS.p, CDS/protein positions, distance, and warnings.
- For high-impact summaries, count by `Annotation_Impact` and by Sequence Ontology effect, but keep transcript multiplicity explicit.

## Gotchas

- A matching species name is not enough; reference sequence names and coordinates must match.
- GFF3 parsing quality varies by provider. If a GTF from the same annotation release is available, prefer it for large genomes.
- If chromosome names differ only by prefixes such as `chr`, do not patch blindly. Normalize only after proving FASTA, GFF/GTF, and VCF refer to the same assembly.
- If many variants are intergenic or chromosome-not-found warnings appear, suspect a reference/annotation/VCF mismatch.
- For compressed VCF output, pipe through `bgzip` and index with `tabix` only when those tools are present.

## Extended Reference

Read `references/snpeff_notes.md` when deciding between pre-built and custom databases, explaining `genome-version`, or handling GFF/GTF/GenBank build details. Read `references/snpeff_config_templates.md` for custom config and directory-layout details.
