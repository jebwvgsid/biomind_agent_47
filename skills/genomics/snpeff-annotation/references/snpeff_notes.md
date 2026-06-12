# snpEff Notes

## What `genome-version` Means

`genome-version` is the snpEff database key passed on the command line:

```bash
java -jar snpEff.jar -v <genome-version> variants.vcf > variants.ann.vcf
```

It must match a genome entry in `snpEff.config`, such as:

```text
project_ref_v1.genome : Project reference v1
```

It also corresponds to database files under `data/project_ref_v1/`. It is not a taxonomy label and should not be guessed from species names.

## Official Doc Points To Preserve

- The official docs show basic annotation as `java -Xmx8g -jar snpEff.jar <genome-version> input.vcf > annotated.vcf`.
- VCF is the recommended input/output format, and snpEff writes annotations into the INFO `ANN` field.
- `java -jar snpEff.jar databases` lists supported genomes.
- `java -jar snpEff.jar download -v <genome-version>` downloads a pre-built database.
- Custom database builds require adding a genome entry to `snpEff.config`, placing annotation and reference files in the expected data layout, and running `build` with the correct format flag.
- GTF 2.2 build flag: `build -gtf22 -v <genome-version>`.
- GFF3 build flag: `build -gff3 -v <genome-version>`.
- GenBank build flag: `build -genbank -v <genome-version>`.
- For FASTA placement, snpEff accepts either `data/genomes/<genome-version>.fa` or `data/<genome-version>/sequences.fa`; gzip-compressed forms are also accepted.
- For GFF/GTF/RefSeq builds, snpEff tries to check CDS/protein sequences when files are supplied. If neither is supplied, builds may fail unless checks are disabled.

## Decision Rules

Use a pre-built database when all of these are true:

- The exact reference assembly and annotation release are known.
- The snpEff database key matches that assembly/release.
- Contig names in the VCF match the database/reference.

Build a custom database when any of these are true:

- The organism/reference is not available as a matching snpEff database.
- The project uses a cultivar, strain, custom assembly, pangenome, or unpublished reference.
- The user needs annotations from a project-specific GFF/GTF rather than the official database release.

Stop and ask when:

- Only species/taxonomy is known, but no reference build or annotation release is provided.
- The annotation source and VCF reference build may differ.
- Non-standard codon tables may apply but no config lines are provided.

## Minimal Preflight Checks

```bash
test -s variants.vcf.gz
java -Xmx2g -jar snpEff.jar -version
java -jar snpEff.jar databases | grep -F "<genome-version>" || true
bcftools view -h variants.vcf.gz | grep '^##contig' | head
```

For custom builds:

```bash
test -s reference.fa
test -s genes.gff3   # or genes.gtf / genes.gbk
grep -m1 '^>' reference.fa
grep -v '^#' genes.gff3 | head
```

## Output Checks

- Header has `##SnpEffVersion`.
- Header has `##INFO=<ID=ANN`.
- Annotated records contain `ANN=`.
- Summary HTML and genes table are present unless summary was disabled.
- Logs do not show widespread missing chromosome or missing transcript warnings.

## Common Failure Modes

- `ERROR: Database not found`: wrong `genome-version`, missing download, or wrong data directory/config.
- Most annotations are intergenic: VCF and annotation coordinates may be from different assemblies.
- Chromosome not found: contig naming mismatch or wrong database.
- Build refuses to save database: CDS/protein check failed or check files are missing.
- GFF3 builds fail or produce poor transcript models: annotation attributes do not meet snpEff expectations; try provider GTF if available.
