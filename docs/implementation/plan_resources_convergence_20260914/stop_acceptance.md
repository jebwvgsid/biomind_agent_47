# Bounded Planner / active stop — FIX acceptance

Parent: e83cec1ef2e8961805cd4099c71056340e308d40. Actual loaded source/fixture hashes are in stop_export_manifest.json and stop_feedback_export_manifest.json (last narrow feedback change). No historical export was reused as FIX evidence.

Public STOP_PLANNING is one complete object with exactly outcome_type and diagnostic. Diagnostic requires stage, error_codes, attempted_changes, stop_reason, uncertainties, unperformed_work. It is mutually exclusive with Plan, Skill and reconciliation fields. The parser recognizes the discriminator; invocation staleness is checked before body admission. Truncation, missing fields, mixed fields and free-text tags do not stop by keyword. The real request includes this protocol, per-request remaining budget and permission to stop before exhaustion. Model diagnostic is source=model_report, verified=false; actual validator issues remain separate. Existing planning_failure → terminalize_planning_failure → TypedPlanner terminal carries it.

The existing CAS call claim has an independent hard maximum of four including first, no Skill-request refund. Fourth valid is accepted, fourth invalid terminalizes, fifth is never dispatched. A/B/A stops after request 3; a compiled candidate differing only by system scope UUID stops after request 2. Exact candidate-owned ID substitutions apply only to comparison views, never persisted approval identity or arbitrary user strings. The bounded seen ledger lives in the existing epoch, includes trusted task/protocol/requirements facts, and retains legacy adjacent comparison when no epoch exists. Replayed call completion and closed-epoch reload do not dispatch or refund.

Guarded commands use the same env -i / Python bootstrap prefix documented in resource_acceptance.md.
```sh
python tests/export_plan_resource_fix.py /tmp/biomind-planfix-v1/stop-final3
# bootstrap arguments:
/tmp/biomind-planfix-v1/stop-final3 -q tests/test_plan_stop_fix.py tests/test_planner_full_replan_convergence_v2.py tests/test_planner_feedback_boundary.py
python tests/export_plan_resource_fix.py /tmp/biomind-planfix-v1/stop-isolated-final
# bootstrap arguments after preserving structured not_evaluated in actual feedback:
/tmp/biomind-planfix-v1/stop-isolated-final -q tests/test_plan_stop_fix.py::test_active_model_stop_with_budget_remaining tests/test_planner_feedback_boundary.py
```
rc=0: 40 passed; rc=0: 10 passed. SOURCE_UNCHANGED=True for both. Development failures: one export omitted requested test files (rc=4), then three legacy no-epoch repeat assertions failed (rc=1); both corrected without weakening assertions. Fixed responses verify wiring, not real model quality. No science executed.

Commit isolation: the original protected provider filter is not in HEAD. The new not_evaluated fact is carried through the shared machine_evidence field instead of editing or committing that overlay-only function. A failed alternate-index merge did not change the branch or real index. The final isolated projection was revalidated: 10 passed, rc=0.
