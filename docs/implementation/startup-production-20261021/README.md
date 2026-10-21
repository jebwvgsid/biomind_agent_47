# BioMind production update — 2026-10-21

Both supported production CLI launchers and the Studio backend now load core `735bacc48c20ff8bf18ffe80b2d1ff8632380865d1960b72fc5b0c71223a93af` (source `15c301f7c1b25ca2a353445f8c40ca1801cec778`). Studio uses backend commit `aad973863840a9a8319574113326b4bf2f91cff8`, Next build `G6ChSLwETBW_siXT1tOHG`. Actual JSON-host startup succeeded at both CLI entries, and the original Studio Main resumed with the same history file and the new loaded build.

The update includes the bounded shared startup admission wait and typed initialization errors, PC1 invalid-value handling (NA instead of false A/B labels), and Studio propagation of early initialization errors without later frames reopening a failed owner. AUDIT.json records the examined branches and explains which earlier work is already integrated or remains an independent draft.

Validation: 57 core regression checks and 67 Studio checks passed. The 53 previously run core checks are tied to unchanged source/compiled artifacts; the 4 PC1 checks and 67 Studio checks ran for these changes. Studio lint/typecheck/build passed. All 270 core compiled artifacts and 1680 frozen Studio build files were verified. Actual deployment acceptance used no model requests.

The native service idle-stop and locked rebind preserved server identity, shared settings and all 30 original session history files byte-for-byte. No task was cancelled or replayed, and no persistent test session was created. Runtime configuration changed only installRoot; the old release and private rollback backups remain available.

This rollout did not modify Windows client binaries; rc8 is the previously accepted version. Client and backend source use separate studio branches of the same repository, with final remote tips verified separately in GIT_RESULT.json. This rollout does not claim a fresh Windows GUI or latency acceptance. The independent latency audit/v9 draft remains separate. Prior natural behavior N1/N2 remain PARTIAL; N3/N4 retain their earlier PASS.

DEPLOYMENT.json contains runtime identities, acceptance results, preservation assertions and evidence hashes. Scripts are the exact reviewed deployment/verification operations, with local paths for this host. Private configuration, credentials and session contents are not included. Final branch tips are verified in `/public/home/liusiqi/03.毕设/BioMind_Startup_Production_20261021/GIT_RESULT.json` after this deployment record is committed and pushed.
