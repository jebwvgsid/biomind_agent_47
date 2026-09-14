# Planner transport budget — FIX acceptance

Planner now owns a dedicated SDK client with max_retries=0 and cache=False. Normal and correction requests share this policy; Generate/Recovery policies are retained. The existing application attempt loop already uses one Planner attempt. A final pre-invoke cancellation/terminal check precedes the actual provider call. A provider exception after cancellation preserves cancellation authority.

Only the bottom provider is a fixed stub in test_plan_transport_fix.py. The actual Planner node, call claim, _ainvoke_node_llm, _ainvoke_llm, invocation registry and run_provider_attempt execute. Fourth-valid: charged claims=4, wire calls=4, four attempt records each ordinal=1 and SDK retries=0, pending approval accepted. Active model stop on response 2: wire calls=2, remaining requests=2, terminal and no later calls. Synthetic transport failure: one claim, one wire call. Cancellation at provider barrier: one claim, zero wire calls. Bad SDK policy: one claim, zero wire calls. Constructor test uses shared retries=3 to prove the dedicated Planner client is still retries=0/cache=False.

No real provider/socket or scientific tool was used. A fixed stub verifies request wiring and policy, not external SDK/provider behavior or real model quality.

Exact-worktree export: /tmp/biomind-planfix-v1/transport-r2
Loaded source signature: b1d88ee169f8aaac65b59a59965c7d71b7ff81004ce397ca33a30c181931caf3
Bootstrap arguments (same guarded environment as resource_acceptance.md):
```
/tmp/biomind-planfix-v1/transport-r2 -q tests/test_plan_transport_fix.py
```
rc=0; 6 passed; SOURCE_UNCHANGED=True. The first development run failed because its synthetic provider replaced the Consultant before triage; the fixed fixture retains real scoped Planner routing and replaces only its bottom provider. No business assertion was weakened.
