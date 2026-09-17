# Runtime and reference preparation

Select BWA (not BWA-MEM2) and Picard for this revision's required capabilities. Alternative implementations require a formal capability/Plan revision. Use the exact runtime bindings and explicit Java/PATH selection in the approved action. Probe results are per subcommand; samtools coverage may be unknown even when faidx and sort are available.

prepare_reference consumes the formal reference_fasta input and produces one immutable directory: reference.fa, reference.fa.fai, reference.fa.amb, reference.fa.ann, reference.fa.bwt, reference.fa.pac, reference.fa.sa, reference.dict. Copy the original reference without changing bytes; execute every required index/dictionary command successfully and retain its stderr/returncode. Emit create_reference_indexes with actual products and reference_bundle_valid.value. The deterministic predicate independently checks products before Commit. Failed or partial products cannot be reported as a complete bundle.

A failed tool check is evidence about this launch, not proof software is absent. Recovery reads failure receipts, ends the old attempt, uses an approved current capability/new generation scope, and performs controlled discovery. Installing or changing scientific implementation requires its own authorization. Scientific Generate actions never install. Installer receives only the concrete environment work order; it may install only the reviewed commands and never accepts scientific outputs.

Input_check may remain accepted only while its input identity, checked scope, provenance and Commit are valid and it is outside the actual rollback set. Keep the input immutable, preserve prior outputs, and do not recreate a helper-owned evidence leaf.
