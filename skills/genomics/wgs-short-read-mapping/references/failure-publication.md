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
missing, accurately block. Do not automatically change the agreed method or start costly
independent indexing; separate work needs existing explicit approval.

Report native return codes, stderr/log locations, produced files and their observed
validation state in ordinary text or JSON. If no product was made, say so and list
only the dependencies actually shown missing. Partial outputs stay explicitly
incomplete. A killed command remains killed; the sender or OOM cause is unknown
unless separately established. Do not turn an output directory into a success
claim or hide the original error behind a reporting error.

Dependency preparation, scoped diagnostic reading and scientific completion are
separate facts. A host may attach its own task receipt without changing this method.
