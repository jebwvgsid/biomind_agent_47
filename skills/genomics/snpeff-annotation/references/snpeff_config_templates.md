# snpEff Custom Database Config Templates

Use this reference only when a custom snpEff database is required. Keep the project config beside the rendered scripts so its `data.dir` resolves predictably.

## Minimal Project Config

Choose a stable project key, not a species guess. The key is used in the config, command line, and data directory.

```text
data.dir = data
project_ref_v1.genome : Project reference v1
```

With that config, use this directory layout:

```text
workdir/
  snpEff.project.config
  data/
    genomes/project_ref_v1.fa
    project_ref_v1/
      genes.gff
```

Use one annotation filename that matches the build mode:

| Source | File under `data/<genome-version>/` | Build command |
|---|---|---|
| GFF3 | `genes.gff` | `build -gff3 -v <genome-version>` |
| GTF 2.2 | `genes.gtf` | `build -gtf22 -v <genome-version>` |
| GenBank | `genes.gbk` | `build -genbank -v <genome-version>` |

The reference may instead be `data/<genome-version>/sequences.fa`; use one layout consistently. Optional `cds.fa` and `protein.fa` belong in `data/<genome-version>/` and strengthen build validation.

## Existing Config Versus Project Config

- Use an installed `snpEff.config` only for a confirmed official database key and matching reference build.
- Use `snpEff.project.config` for a project-specific FASTA plus GFF3/GTF/GenBank build.
- Do not edit a shared conda installation config in place. Supply `-c /path/to/snpEff.project.config` or use the renderer's generated project config.
- Keep `data.dir` relative only when the command runs from the config's directory. Otherwise set it to an explicit project-local absolute path.

## Extra Config Lines

Only add `--config-line` when the reference provider or user supplies the exact required setting. Typical reasons include an explicitly documented non-standard genetic code for an organelle, plastid, bacterium, or mitochondrial contig.

Before adding such a line, confirm all three facts:

1. The contig or genome uses a non-standard code.
2. The provider documents the exact snpEff configuration syntax and table name.
3. The VCF, FASTA, and annotation use the same contig names.

If any fact is unknown, ask the user rather than guessing a codon table or adding unchecked-build flags. `-noCheckCds` and `-noCheckProtein` are a user-accepted fallback only after recording why validation inputs are unavailable.

## Preflight Before Build

```bash
test -s reference.fa
test -s genes.gff3  # or genes.gtf / genes.gbk
grep -m1 '^>' reference.fa
grep -v '^#' genes.gff3 | head -1
```

Compare a small sample of contig names from the VCF, FASTA, and annotation before building. A prefix mismatch such as `chr1` versus `1` is not permission to rewrite files blindly; establish that they represent the same assembly first.
