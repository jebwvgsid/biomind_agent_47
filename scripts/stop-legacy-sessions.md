# Reviewed old conversation shutdown

This tool is for an operator who has explicitly authorized stopping old BioMind
conversations, including interrupting their current model turn. It does not
require task-idle confirmation after that authorization. This authorization is
separate from approving a production entrypoint switch or cancelling unrelated
Slurm workloads.

Use the versioned deployment bundle's `run-reviewed-legacy-stop.py` from the real
login host after closing old desktop windows (their reconnect can start another
service). The wrapper routes to the recorded host over SSH, then runs the existing
read-only maintenance preflight. No host key checks are disabled. It emits a
receipt path on shared storage; loss of the SSH reply is UNKNOWN, not success.

The reviewed plan pins PID, start ticks, executable and command digest from the
operator's earlier inventory. A changed identity stops the entire validation
before signalling any target. Only exact BioMind executed script paths qualify;
an argv substring, resource reference, process name or shared Node executable
does not qualify. Unknown old rows are reported but not signalled. New or
unreviewed processes are left for the post-stop audit.

All eligible targets are opened with pidfd and checked again before signals.
The host kernel must support pidfd, and the Python build must expose both
`os.pidfd_open` and `signal.pidfd_send_signal`. This site's `/usr/bin/python3`
has those interfaces; its conda Python 3.12 build does not. There is no fallback
to an unchecked PID or process-group kill.

Shutdown order: service holder TERM and native flush grace, any surviving
identified backend TERM, then identified independent Main/relay/domain bridge
TERM. `forceAfterGrace: true` allows KILL only on those same pinned targets
after the grace period. No `pkill`, `killpg`, `scancel`, login-process termination,
lock removal, credential access, transcript deletion or settings writes occur.
Scientific Slurm jobs can outlive the stopped owner and remain part of the audit.

An abnormal exit may retain the existing writer claim and keep a transcript
read-only until separately verified recovery. Do not fabricate a clean-close
receipt or delete a claim merely because the PID exited. These shutdown scripts
never label themselves as successful adoption or a production switch.

The existing host audit and resource locks still decide admission. Approval to
stop old conversations does not set `hostInventoryConfirmed`, waive unreadable
processes, mark inventory complete or populate `maintenanceApproved`.

Isolated test command:

```sh
/usr/bin/python3 -B -m unittest discover -s tests -p test_stop_legacy_sessions.py -v
```

The tests use disposable child processes and a test-only namespace stub. They
verify actual TERM/KILL through pidfd, service-before-owner shutdown, identity
changes, a replacement service, unrelated commands, unsupported Python builds,
idempotency, no-force survivors, and preservation of history and writer files.
