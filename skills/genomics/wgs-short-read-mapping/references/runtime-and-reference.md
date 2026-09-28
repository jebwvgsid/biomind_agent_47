# Runtime and reference preparation

This method uses BWA 0.7-compatible indexing (not BWA-MEM2) and Picard dictionary
creation. Alternative implementations require an explicit method decision. Use
verified executable paths and Java/PATH selection. An available samtools faidx
or sort does not establish support for the separate samtools coverage command.

The complete fixed-layout reference bundle contains `reference.fa`,
`reference.fa.fai`, `reference.dict`, and `reference.fa.{amb,ann,bwt,pac,sa}`.
Copy the original FASTA byte-for-byte to a new project bundle directory before
building indexes; do not mutate the user's source. Run BWA index, samtools faidx
and Picard CreateSequenceDictionary as shown in mapping-methods.md. Retain each
return code and stderr. Dictionary URI must identify the final reference location,
with standard file URI escaping. Compare contig names, lengths and sequences
between FASTA, FAI, dictionary and BWA products; mere file presence is insufficient.

No automatic installation or fallback implementation occurs. Missing tools and
failed launches are different facts. Preserve failed/partial products and logs;
do not label an incomplete bundle successful or replace existing user results.
The optional BioMind immutable-publication helper is owned by the host extension;
it is not required to execute these documented native scientific commands.

## Java selection and failed-attempt scratch

For a bound executable shaped as `<environment>/bin/java`, derive the environment
prefix with two parent operations. One parent is the bin directory, not JAVA_HOME:

```bash
JAVA_PREFIX="$(dirname "$(dirname "$JAVA_BIN")")"
export JAVA_HOME="$JAVA_PREFIX"
export PATH="$JAVA_PREFIX/bin:/usr/bin:/bin"
test -x "$JAVA_HOME/bin/java"
```

For another layout, inspect the actual bound installation before choosing its
JAVA_HOME. Do not fall back silently to ambient Java. Keep scratch on failure:

```bash
cleanup() {
    rc=$?
    if [ "$rc" -eq 0 ]; then
        rm -rf -- "$WORK_DIR"
    else
        printf 'Failed-attempt scratch retained: %s\n' "$WORK_DIR" >&2
    fi
}
trap cleanup EXIT
```

A later recovery may inspect that scratch but cannot adopt it as a successful
BAM without recorded input identity and the normal result checks. SIGKILL
itself can prevent the trap from running; preservation is not a success claim.
