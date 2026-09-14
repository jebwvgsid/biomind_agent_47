# Cancellation admission — FIX acceptance

Parent: 034194846b833467b90a7aaabf25eb6b9caa3a6b. Exact loaded signature d035b922f3ccfd1a992254b47b1ec1a7ec31547829914f15ae492a6c7cdc0276; per-file/fixture hashes in cancel_export_manifest.json.

Existing task status/cancellation receipt and current-revision terminal planning_failure revoke Planner authority. Checked at entry, fresh claim, existing state lock for claim CAS, immediately before dispatch, after await before parsing, and under atomic Plan admission lock before allocation/approval writes. Cancellation takes precedence over CAS-conflict redispatch. The cancelled task receives only a discarded-response diagnostic; it does not acquire a new Plan or approval. Reloading the cancelled task returns TypedPlanner terminal without a request.

Deterministic barriers call the real user-cancellation transaction, not sleep or mocked validators. Results (actual provider requests / charged claims / Plan-or-approval object writes): pre-claim 0/0/0; race before claim lock 0/0/0; after claim before dispatch 0/1/0; after validators before atomic Plan admission 1/1/0. A charged but cancelled dispatch reservation is not refunded. Post-await valid, invalid and STOP responses are discarded before validator entry, one request each and zero later requests. Admission-before-cancel preserves accepted snapshot history and closes Generate eligibility; Generate provider calls=0. Nonsemantic generation during await still admits C_FIX, one request. No OS/process cleanup assertion is made; no process enumeration or scientific execution occurred.

Actual command uses env -i/bootstrap prefix in resource_acceptance.md:
```sh
python tests/export_plan_resource_fix.py /tmp/biomind-planfix-v1/cancel-reload-final
# bootstrap arguments:
/tmp/biomind-planfix-v1/cancel-reload-final -q tests/test_plan_cancel_fix.py tests/test_plan_stop_fix.py::test_closed_epoch_reload_and_claim_replay_are_not_new_calls
```
rc=0: 10 passed, SOURCE_UNCHANGED=True. Earlier rc=1 was the fixture selecting the final __interrupt__ event instead of the emitted Planner terminal; real cancellation/no-allocation assertions already passed. Selection corrected, strengthened by real cancelled-task reload and claim-lock barrier, no assertion weakened.
