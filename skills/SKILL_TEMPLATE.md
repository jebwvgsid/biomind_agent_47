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
