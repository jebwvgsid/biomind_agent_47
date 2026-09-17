# One real controlled host inspection

The production S1 discovery/adapter/probe attempted exactly one controlled inspection of the existing `bio_gatk` prefix, using the copied S3 `inspect_vcf_samples` contract. Only `bcftools --version` and `bcftools query --help` were admitted. No GWAS inputs were read.

The outcome was **UNKNOWN / RUNTIME_INSPECTION_TIMEOUT after 60 seconds**. The production handler attempts process-group cleanup if its parent process is still running, then raises on timeout; it does not return partial stdout/stderr or a child exit code. Those facts are unavailable in this record, so the result must not be described as a NETLINK error, missing bcftools, or successful inspection. No repeated probe was launched.

`result.json` records the exact generated bwrap argv, read roots, environment, current contract/source hashes, and typed result. `summary.json` separates actual outcome from the fixture PASS, which only confirms correct unverified/no-install/no-registry-write behavior. `command.txt` and `harness_identity.json` identify the reproducible harness. Re-running the command is a new real inspection, not a read-only examination of this evidence.

The only bootstrap exception is one exact pre-generated bwrap argv with executable `/usr/bin/bwrap`, cwd `/`, and empty inherited environment. Existing network, arbitrary subprocess, protected config/credential read, and non-temporary write guards are unchanged. The bwrap user/network/PID/IPC/UTS namespaces, readonly roots, private proc/dev/tmp, no-capability setting, and production timeout remain intact. Metadata copies and the candidate exist under `/tmp`; the original software registry and production worktree were not read or mutated by this harness.
