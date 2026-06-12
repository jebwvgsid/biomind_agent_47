# BioMind Skill Template

`SKILL.md` must be self-contained enough for the agent to act after the skill is selected. Keep the YAML small, but keep the Markdown body useful. Do not move core operating instructions entirely into `references/`.

```text
skills/<domain>/<subdomain>/<skill-id>/
  SKILL.md
  references/   # optional: long docs the agent should read before deciding
  assets/       # optional: static templates or example outputs
  scripts/      # optional: deterministic executables
```

## Minimal Frontmatter

```markdown
---
name: weekly-report
description: Generate weekly reports from structured project data. Use when the user asks for a weekly/monthly report or recurring summary; do not use for one-off statistical analysis.
---
```

Rules:

- `name` is required and should match the skill directory basename when practical.
- `description` is required and should be specific. It must say when to use the skill and, when useful, when not to use it.
- Keep `description` under 1024 characters.
- Do not add `version` unless versioning is actually used.
- Do not add `tags`, `task_types`, `stage_tags`, `inputs`, or `outputs` unless the code relies on them for a specific workflow.
- Do not add `runtime` unless BioMind must enforce or install a specific environment before using the skill.

## Optional Frontmatter

Use optional fields only when they solve a real routing or execution problem:

```markdown
---
name: local-blast
description: Run BLAST+ locally against custom FASTA databases. Use for private, custom, or large batch sequence searches; do not use for quick public single-query BLAST.
legacy_ids: [bio-local-blast, blast-plus]
compatibility: Requires BLAST+ on PATH or a conda environment that provides blastn/blastp.
allowed-tools: Bash Read
metadata:
  source: openclaw
---
```

- `legacy_ids`: keep old names working after migration.
- `compatibility`: short human-readable dependency note.
- `allowed-tools`: only if the runtime honors tool restrictions.
- `metadata`: only for provenance or priority hints that are useful later.

## Optional Runtime Contract

Add `runtime` only when BioMind must verify or install an environment before
using the skill. Prefer declarative, reproducible runtime metadata over prose
instructions buried in the body.

```yaml
runtime:
  default_env: example-env
  env_runner: conda run -n example-env python
  language: python
  packages: [numpy, pandas]
  capabilities: [example-analysis]
  healthcheck: conda run -n example-env python scripts/run_example.py --help
  install_policy: prefer-declared
  install_command: mamba create -n example-env -c conda-forge numpy pandas -y
  install_fallbacks:
    - conda create -n example-env -c conda-forge numpy pandas -y
```

Runtime policy:

- `prefer-declared`: the installer tries declared commands first. If the real
  error proves that an extra package is missing, it may make a generic repair in
  the same target environment, but must pass the skill healthcheck before
  registration. This is the default for ordinary skills.
- `declared-only`: the installer may execute only declared commands or an
  `env_file` command generated from the metadata. Use this for fragile or
  version-sensitive runtimes such as R/Bioconductor, CUDA, system-backed tools,
  or workflows where ad-hoc fallback can silently corrupt the environment.
- `env_file`: path relative to the skill directory for a conda/mamba environment
  file. Use it when the environment should be reproducible.

Do not mark a skill `declared-only` unless all known install paths and approved
fallbacks are listed. If you omit install commands entirely, normal missing
package repair remains allowed, but the environment still must pass the
healthcheck before BioMind treats it as installed.

## Body Structure

After the YAML, write enough Markdown instructions that the selected skill is usable without opening any other file:

```markdown
# Local BLAST

Use this skill for local BLAST searches against custom or downloaded databases.

## When To Use

- Search private/custom genome databases.
- Run batch sequence searches.

Do not use this for one quick public NCBI search; use `remote-blast` instead.

## Workflow

1. Verify query FASTA and database type.
2. Build the database with `makeblastdb` if needed.
3. Run `blastn`, `blastp`, or the correct BLAST+ program.
4. Summarize top hits, e-values, identity, and coverage.

## Related Skills

- `remote-blast` - quick public NCBI searches
- `sequence-similarity` - PSI-BLAST, HMMER, reciprocal best hits

## Extended References

- `references/usage-guide.md` - optional detailed guide
- `examples/search.sh` - optional runnable example
```

## What Goes Where

- `SKILL.md`: agent-facing core instructions, when to use/not use, essential workflow, common commands, related skills.
- `references/`: longer background, API documentation, Chinese/English user guides, edge cases.
- `assets/`: static templates or reusable output skeletons.
- `scripts/`: deterministic executables or wrappers.

`usage-guide.md` can be user-readable, but it is still a reference file. The agent should not need it for the basic operation of the skill.
