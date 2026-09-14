# Concrete public stop example and independent epoch reload

The shared public STOP_PLANNING example contains concrete complete diagnostic values, without placeholders. It illustrates a missing local role association, records uncertainty, and lists unperformed scientific work. Actual validator facts remain separate; model diagnostic is not verified evidence.

Guarded export /tmp/biomind-planfix-v1/stop-example-r2; tests/test_plan_epoch_reload_fix.py and tests/test_plan_stop_fix.py: rc=0, 17 passed, source unchanged. A separately launched guarded process selects test_read_closed_epoch_without_refund_or_dispatch: rc=0, 1 passed. Exact commands are in stop_reload_commands.json. A module process nonce differs between writer and reader; numeric PIDs alone are not used as proof because separate tool PID namespaces both returned 3 in an earlier run.

The actual state contract store tree is copied only from a new synthetic task. The reader loads the four-call exhausted epoch, replays the recorded completion twice idempotently, fails a new real claim, classifies TypedPlanner terminal, and confirms no refund or state change. No provider or scientific process is called by the reader. The final combined receipt repeats the process separation on the final source.
