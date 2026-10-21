# Studio startup admission diagnostics

Before the Main owner sent `ready`, its unscoped error frame did not match a
pending control request. A subsequent child exit replaced a useful `BUSY` code
with `BIOMIND_OWNER_EXITED`, so Studio could not distinguish lock contention from
an unknown owner loss.

The transport now rejects startup immediately with the public `BUSY` or
`PI_INITIALIZATION_FAILED` code. For `BUSY`, only the two resource-lock stages and
an integer `waitMs` from 0 through 5000 are retained. Unknown startup codes become
`BIOMIND_START_FAILED`; raw messages, paths, stacks, and other fields are omitted.
The existing HTTP error envelope reports that no input was admitted. A later
child close cannot replace that startup result, and no prompt is written or
replayed. Further frames in the same stdout chunk are ignored after termination,
so a late `ready` frame cannot assign a new identity to the failed owner.
Post-ready responses retain the previous transport behavior.

Regression coverage uses the real adapter with a simulated child process and
its actual stdout/stdin streams. It covers admission timeout, resource-lock
contention, later child exit, diagnostic filtering, and a pending prompt that
must not be sent. Adjacent tests cover resume serialization, controls, package
management, and RPC startup. The resume fixture now supplies the existing
package-management gate mock, so it reaches its original concurrency assertions.

The isolated worktree starts from production Studio source commit
`573432a7e5f7d1099aacd2b29fcb5e2385be8793`. Its backend tree is byte-identical to
service maintenance commit `59f269bcd8027d40b8685feffd14a656176d5817` before this
change. The Windows rc8 branch remains separate. Build output uses `.next-pkg`;
the existing production backend and service configuration are not edited by
these source/build steps. Production deployment must use the normal idle-stop,
locked rebind, and ensure workflow, followed by actual Main `loaded_build`
verification and restoration of the original session.

See `VALIDATION.json` for commands, results, evidence locations, and build
identity. Runtime dependencies were reused from the same pinned installation as
the current production backend. No package upgrade or model request was needed.
