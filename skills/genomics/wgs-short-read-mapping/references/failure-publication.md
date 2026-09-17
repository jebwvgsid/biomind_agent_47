# Failure evidence publication
## Capability and failure reporting boundaries

FASTA random access (`samtools faidx`), BWA indexing (`bwa index`), sequence
dictionary creation (an explicitly verified compatible dictionary tool), and
BAM duplicate marking are different capabilities. The coarse runtime
healthcheck above is not proof of a dictionary capability: `sambamba markdup`
handles BAM duplicates and cannot replace dictionary creation. Finding an
environment directory or a command entry is not a subcommand/version check.
Use the approved runtime binding and an explicitly selected compatible tool;
never guess an environment path, silently substitute tools, or install one.
If the approved step requires a whole bundle and a required capability is
missing, accurately block. Do not automatically split the Plan or start costly
independent indexing; separate work needs existing explicit approval.

For framework evidence use the current `approved_effect_emission_template`
marker, once in a Python block before native fact values. Pass the actual
reference path as `sys.argv[1]` and observed missing entries as later argv.
After the **real** dependency check, a no-product branch can use:

```python
# BIOMIND_EFFECT_HELPER
import sys
facts = {"explicit_side_effects": {"create_reference_indexes": {
    "reference_resource_ref": sys.argv[1], "index_artifacts": [],
    "status": "blocked", "missing_commands": sys.argv[2:]}}}
print(emit_effect_evidence(facts))
raise SystemExit(1)
```

This is a branch example, not a capability probe or a success claim. The marker
is expanded by BioMind before candidate checks and approval; do not copy the
serializer source or nest JSON shell substitutions. For partial output list
only actually produced products and their incomplete/unverified status; for
tool failure retain its real rc/stderr. If publication itself fails, preserve
both the original failure and the publisher error. A directory reader refusal
does not prove corrupt data; inspect only offered readable log/evidence refs.

Use this skill to convert paired-end WGS FASTQ into duplicate-marked coordinate-sorted BAMs. The downstream caller decides whether duplicates should be marked or removed; for variant calling, default to **mark and keep**.
