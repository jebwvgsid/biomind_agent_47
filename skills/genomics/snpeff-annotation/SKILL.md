---
name: snpeff-annotation
description: Build or use snpEff databases and annotate VCF variants with ANN effects. Use when the user asks for SNP/indel functional annotation with snpEff, including existing pre-built databases, custom GFF3/GTF/GenBank database builds, ANN interpretation, summary HTML, and safe database/version selection. Do not guess species databases, genome-version names, chromosome names, codon tables, or unchecked-build options from taxonomy labels alone.
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
- Do not add `-noCheckCds` or `-noCheckProtein` by default. Use unchecked builds only when CDS/protein FASTA are unavailable and the user accepts the lower confidence database.
- Do not hard-code codon tables. If organellar, bacterial, plastid, mitochondrial, or other non-standard codon usage may apply, ask for or inspect the required snpEff config lines.

## Official Documentation Anchors

- Running and annotation command pattern: `https://pcingola.github.io/SnpEff/snpeff/running/`
- Input/output and ANN field: `https://pcingola.github.io/SnpEff/snpeff/inputoutput/`
- Database building: `https://pcingola.github.io/SnpEff/snpeff/build_db/`

## Reusable Renderer

Render scripts for annotating with an existing database:

```bash
python skills/genomics/snpeff-annotation/scripts/render_snpeff_annotation_scripts.py \
  --vcf variants.vcf.gz \
  --genome-version project_or_official_database_key \
  --snpeff-jar /abs/path/snpEff.jar \
  --config /abs/path/snpEff.config \
  --outdir snpeff_annotation
```

Render scripts for building a custom GFF3 database and then annotating:

```bash
python skills/genomics/snpeff-annotation/scripts/render_snpeff_annotation_scripts.py \
  --vcf variants.vcf.gz \
  --genome-version project_reference_release \
  --snpeff-jar /abs/path/snpEff.jar \
  --reference-fasta /abs/path/reference.fa \
  --annotation-gff3 /abs/path/annotation.gff3 \
  --outdir snpeff_annotation
```

The renderer writes `00.preflight.sh`, `01.prepare_custom_database.sh` when custom inputs are provided, `02.build_database.sh` for custom builds, `02.download_database.sh` for existing databases, and `03.annotate_vcf.sh`.

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
5. Provide `cds.fa` and/or `protein.fa` when available. If missing, do not suppress checks unless the user accepts that database validation will be weaker.
6. Annotate the VCF only after the database build succeeds.

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

Read `references/snpeff_notes.md` when deciding between pre-built and custom databases, explaining `genome-version`, or handling GFF/GTF/GenBank build details.
