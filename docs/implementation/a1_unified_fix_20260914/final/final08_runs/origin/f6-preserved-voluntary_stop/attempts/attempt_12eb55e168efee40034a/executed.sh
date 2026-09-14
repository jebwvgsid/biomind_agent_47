#!/bin/bash
set +e
: > /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-h97h2bub/task_outputs/fix-f6-voluntary_stop/attempts/attempt_12eb55e168efee40034a/stdout.log
: > /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-h97h2bub/task_outputs/fix-f6-voluntary_stop/attempts/attempt_12eb55e168efee40034a/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-h97h2bub/task_outputs/fix-f6-voluntary_stop/attempts/attempt_12eb55e168efee40034a/approved.sh > /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-h97h2bub/task_outputs/fix-f6-voluntary_stop/attempts/attempt_12eb55e168efee40034a/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-h97h2bub/task_outputs/fix-f6-voluntary_stop/attempts/attempt_12eb55e168efee40034a/approved.sh > /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-h97h2bub/task_outputs/fix-f6-voluntary_stop/attempts/attempt_12eb55e168efee40034a/stdout.log 2> /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-h97h2bub/task_outputs/fix-f6-voluntary_stop/attempts/attempt_12eb55e168efee40034a/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-h97h2bub/task_outputs/fix-f6-voluntary_stop/attempts/attempt_12eb55e168efee40034a/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_12eb55e168efee40034a","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-h97h2bub/task_outputs/fix-f6-voluntary_stop/attempts/attempt_12eb55e168efee40034a/exit_status.json.tmp.$$'
mv '/tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-h97h2bub/task_outputs/fix-f6-voluntary_stop/attempts/attempt_12eb55e168efee40034a/exit_status.json.tmp.$$' /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-h97h2bub/task_outputs/fix-f6-voluntary_stop/attempts/attempt_12eb55e168efee40034a/exit_status.json
exit "$rc"
