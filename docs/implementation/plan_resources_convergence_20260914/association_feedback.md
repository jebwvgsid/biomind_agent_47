# Complete role repair feedback through the preserved provider boundary

The existing protected provider projection strips nested step_id control keys. Inspection of the actual request found expected.role_association preserved capability_id and role but omitted the association step value. This is a localized feedback projection issue, not loss of the candidate/history or Skill context.

Resource-role validator issues now carry their explicit step_id at the standard issue boundary. The existing projection emits step_key. Public prompt and validator instruction explicitly say to place that step_key in role_associations[].step_id. The protected filter itself is unchanged. The exact next provider request test asserts all three step keys, matching capability IDs, role and instruction, along with structured not_evaluated downstream checks.

Guarded export association-feedback-fix, test arguments: tests/test_plan_stop_fix.py tests/test_planner_feedback_boundary.py. rc=0, 23 passed, SOURCE_UNCHANGED=True. Full source/fixture/result hashes attached. Model quality is not inferred from the fixed provider.
