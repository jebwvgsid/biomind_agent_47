# BioMind startup follow-up — 2026-10-08

This change follows prerequisite `893e87576cc1e34169c5e090d9ac9a5ac8789ed9`
and startup repair `261f96a8166258a85565d04494d9090c5870daa3`.
It is limited to recovery configuration, `/status`, and project creation errors.
Node remains 22.22.0; all three Pi packages remain pinned to 0.87.1.

| Boundary | Cause | Reused interface and minimal change |
| --- | --- | --- |
| Persisted-session recovery hint | The terminal adapter only printed project and session arguments, losing the runtime config and selected executable/model record. | Reuse the host's normalized `BIOMIND_PI_CONFIG_PATH`, `BIOMIND_PI_NODE`, `BIOMIND_PI_MODEL_RECORD` and existing shell quoting; emit the supported BioMind flags. |
| `/status` during backend failure | It displayed request state or `ctx.isIdle()` without consulting the backend details already supplied by the extension. | Reuse `backendLabel()` until `backend.state === 'ready'`; then retain the existing request-status function. |
| Project creation rejects HOME | `createProject()` replaced every failure with a generic code, and the startup menu guessed a permission problem. | Preserve known `ProjectDirectoryError.code/message` in the existing receipt and display the reason; retain the fallback for unknown errors. |

The actual Node executable is retained even when originally selected through a
default: it identifies the interpreter that produced the session. The optional
model-record path retains the existing selection source. The explicit runtime
config is necessary for custom agent, domain data, memory and other runtime
settings. The recovery command uses the already-persisted absolute session file;
it does not promise scientific-job continuation.

Only the affected locked compatibility patch revision and hashes change; Pi's
package version, default configuration layout and authentication ownership stay
unchanged. Generated native and JSON build outputs must come from the complete
`npm run build` command.

The exact CLI `--session` also overrides a `continueSession: true` default in
that same config file. Explicit CLI `--session` plus `--continue`, and explicit
project/session ownership conflicts, still fail. No session is silently rebound.

## Validation

All directed tests ran against the complete proposed source in the detached
checkout `/tmp/biomind-startup-candidate-20261008`, based on the prior repair.
That checkout reused the installed pinned Node/dependencies; its product sources
and generated bundles did not come from the dirty main worktree.

Evidence: `/public/home/liusiqi/03.毕设/BioMind_Startup_Followup_20261008/evidence/`.

| Result | Check | Evidence |
| --- | --- | --- |
| PASS | Isolated complete `npm run build` | `candidate-build.log` |
| PASS | Backend/status UI: 28 tests | `status-tests.log` |
| PASS | Project creation and entry boundaries: 11 tests | `project/unit.log` |
| PASS | HOME-create refusal in an actual controlling PTY: 5 checks | `project/pty/results.json` |
| PASS | Exact-session/config-continue precedence and retained explicit conflicts: 1 test | `recovery-settings-tests.log` |
| PASS | Compatibility installer and terminal handler: 17 tests, including .5 to .6, exact revert/reapply and quoting | `compat-tests.log` |
| PASS | Original launch and generated recovery command in actual PTYs: 8 checks | `recovery/summary.json` |
| PASS | Installed compatibility target hashes, plus 14 affected emitted files/source maps equal the isolated build | `installed-compat-check.json`, `artifact-hashes.json` |
| FAIL, preserved | Main-worktree complete build attempts ended with 137, both sandboxed and escalated; no compiler error was printed | `build-receipt.json`, `installed-build*.log` |
| PASS | The same native-bundle build script completed separately in the installed tree; resulting files match isolated outputs | `installed-native-bundle.log`, `artifact-hashes.json` |
| NOT_RUN | Startup performance comparison, network dependency reinstall, paid models, original TAD and large scientific analyses | Outside this focused validation |

The recovery fixture uses a temporary persisted session and a protected execution
profile. Its first launch selected an explicit config, Node and model record.
The generated command is passed unchanged to `bash -c` from another directory;
the tester adds no business arguments or business configuration environment
variables. The child process environment, domain argv and persisted domain
binding establish the custom agent/data paths and model source. Original and
final message counts remain two, and offline checks record no provider network
attempt. This proves context preservation, not resumability of scientific work.

Two earlier recovery-fixture runs are retained separately: the first incorrectly
waited for the large new-session welcome text instead of the compact historical
session header; after correcting the fixture, another run exited by SIGKILL
without confirming readiness. The latter cause was not established. The final
instrumented run passed without timing out. No startup performance conclusion
is drawn from these runs or the build terminations.

The two old scientific lifecycle failures (backpressure cancellation timing and
crash/reconnect worker-terminal marking) remain separately tracked in the
previous startup audit and were not rerun or modified here. The original
terminal EIO trigger is still unconfirmed. The user's original TAD session/job
was neither reopened nor resumed.

The actual local launcher uses the updated compiled host/native bundle and
observer.6 adapter on its next launch. No push, remote deployment, or running
user-session restart is part of this change.
