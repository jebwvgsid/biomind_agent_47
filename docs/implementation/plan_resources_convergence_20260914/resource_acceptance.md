# Resource slice — FIX acceptance

Base HEAD: `f78bb10bb3f12059efaf911916940440cecdd73d`. Current source export (including the five original protected overlays) signature: `916460b411a3763ca79b1abb0cbfd49f1dcb4e52d73fb777d90f8f1503b78009`. Full per-file/source/fixture hashes: resource_export_manifest.json.

Public addition: each proposed artifact may declare role_associations entries with exactly step_id, capability_id and role. These are local, explicit associations with a role declared by the selected capability. resource_binding remains existing-authority reuse only. A role is never registered as a global instance alias. C_FIX changes only these three entries; all six original scientific steps and required outputs remain.

Bindings survive real candidate scope, deterministic resource/effect/dataflow admission, atomic allocation, approval, immutable snapshot, independent process reload and actual Generate request. Real Generate returns action_ready and SNPEFF_DB is the approved formal path. Workspace is not precreated. Provider and prerequisite scientific observations/inputs are explicitly synthetic; returned shell code is never executed. Two independent workspace projections remain distinct; cross-instance binding is rejected.

Required mutable coverage uses validated ordered MUTATE state/evidence obligations; immutable required outputs retain PRODUCE requirements. Annotation freezes required_state=snpeff_database_ready. New association snapshots require reader 1.2.2; legacy minimum 1.2.1 eligibility remains explicit.

Commands (working directory repository):
```sh
python tests/export_plan_resource_fix.py /tmp/biomind-planfix-v1/resource-final
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 TMPDIR=/tmp/biomind-planfix-v1/runtime BIOMIND_RESOURCE_AUDIT_RESULTS=/tmp/biomind-planfix-v1/results BIOMIND_OFFLINE_RESULTS=/tmp/biomind-planfix-v1/resource-final-pytest.json /public/home/liusiqi/anaconda3/envs/biomind/bin/python /tmp/biomind-planfix-v1/resource-final/tests/offline_managed_bootstrap.py /tmp/biomind-planfix-v1/resource-final -q tests/test_plan_resource_fix.py tests/test_planner_resource_protocol_v2.py
```
rc=0: 25 passed, 4 subtests passed, SOURCE_UNCHANGED=True. A second guarded Python process runs only tests/test_plan_resource_fix.py::test_independent_process_frozen_reload with result path resource-reload-pytest.json: rc=0, 1 passed. This is resource FIX PASS, not the old witness PASS. The historical nine-failure fixture ledger remains in the old audit untouched. Active stop/convergence/cancellation are subsequent slices.
