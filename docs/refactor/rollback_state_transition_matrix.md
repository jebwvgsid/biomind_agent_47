# Batch 7 Rollback State Transition Matrix

| Old state | RecoveryDecision | New state | Move plan cursor | Artifact effect | Budget |
|---|---|---|---|---|---|
| current action failed | `RETRY_CURRENT_ACTION` | current step `retry_pending` | no | none until new evidence | `execution_retry_count` |
| upstream producer passed, consumer failed | `ROLLBACK_UPSTREAM_STEP` | producer `retry_pending`; consumer `waiting_upstream`; descendants `invalidated` | yes, producer | producer/affected versions stale; explicit bad IDs invalid | `upstream_rollback_count` |
| Renderer stage passed/failed | `REWIND_MANIFEST_STAGE` | target stage `retry_pending`; stage descendants `invalidated` | Plan cursor unchanged; stage cursor moves | affected stage versions stale/invalid | `stage_rewind_count` |
| required artifact has no producer | `PLAN_PATCH_REQUEST` | current step repairing; Plan unchanged until approved patch | no | none | `plan_patch_count` |
| required user/input evidence absent | `BLOCKED` | task/branch blocked | no | none | none |
| completion contract satisfied | `COMPLETE_STEP` | current step passed; ready child running | yes, ready child | active versions retained | none |
| final contract satisfied | `COMPLETE_TASK` | task completed | cleared/unchanged | all active heads must remain valid | none |

`waiting_upstream` and `invalidated` are recoverable, not terminal blocked states. A repaired producer releases only children whose complete dependency set is valid.

