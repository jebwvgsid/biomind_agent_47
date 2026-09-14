#!/bin/bash
set +e
: > /tmp/biomind-a1-unified-final-05/protocol-helper-shared/scratch/biomind-plan-local-graph-0wbo86h7/task_outputs/fix-f6-recovery/attempts/attempt_953505de272290d5692e/stdout.log
: > /tmp/biomind-a1-unified-final-05/protocol-helper-shared/scratch/biomind-plan-local-graph-0wbo86h7/task_outputs/fix-f6-recovery/attempts/attempt_953505de272290d5692e/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-a1-unified-final-05/protocol-helper-shared/scratch/biomind-plan-local-graph-0wbo86h7/task_outputs/fix-f6-recovery/attempts/attempt_953505de272290d5692e/approved.sh > /tmp/biomind-a1-unified-final-05/protocol-helper-shared/scratch/biomind-plan-local-graph-0wbo86h7/task_outputs/fix-f6-recovery/attempts/attempt_953505de272290d5692e/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-a1-unified-final-05/protocol-helper-shared/scratch/biomind-plan-local-graph-0wbo86h7/task_outputs/fix-f6-recovery/attempts/attempt_953505de272290d5692e/approved.sh > /tmp/biomind-a1-unified-final-05/protocol-helper-shared/scratch/biomind-plan-local-graph-0wbo86h7/task_outputs/fix-f6-recovery/attempts/attempt_953505de272290d5692e/stdout.log 2> /tmp/biomind-a1-unified-final-05/protocol-helper-shared/scratch/biomind-plan-local-graph-0wbo86h7/task_outputs/fix-f6-recovery/attempts/attempt_953505de272290d5692e/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-a1-unified-final-05/protocol-helper-shared/scratch/biomind-plan-local-graph-0wbo86h7/task_outputs/fix-f6-recovery/attempts/attempt_953505de272290d5692e/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_953505de272290d5692e","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-a1-unified-final-05/protocol-helper-shared/scratch/biomind-plan-local-graph-0wbo86h7/task_outputs/fix-f6-recovery/attempts/attempt_953505de272290d5692e/exit_status.json.tmp.$$'
mv '/tmp/biomind-a1-unified-final-05/protocol-helper-shared/scratch/biomind-plan-local-graph-0wbo86h7/task_outputs/fix-f6-recovery/attempts/attempt_953505de272290d5692e/exit_status.json.tmp.$$' /tmp/biomind-a1-unified-final-05/protocol-helper-shared/scratch/biomind-plan-local-graph-0wbo86h7/task_outputs/fix-f6-recovery/attempts/attempt_953505de272290d5692e/exit_status.json
exit "$rc"
