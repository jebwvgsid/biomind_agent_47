#!/bin/bash
set +e
: > /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-mch17f71/task_outputs/fix-f6-recovery_event_failure/attempts/attempt_677ebadbb05be761dfb0/stdout.log
: > /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-mch17f71/task_outputs/fix-f6-recovery_event_failure/attempts/attempt_677ebadbb05be761dfb0/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-mch17f71/task_outputs/fix-f6-recovery_event_failure/attempts/attempt_677ebadbb05be761dfb0/approved.sh > /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-mch17f71/task_outputs/fix-f6-recovery_event_failure/attempts/attempt_677ebadbb05be761dfb0/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-mch17f71/task_outputs/fix-f6-recovery_event_failure/attempts/attempt_677ebadbb05be761dfb0/approved.sh > /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-mch17f71/task_outputs/fix-f6-recovery_event_failure/attempts/attempt_677ebadbb05be761dfb0/stdout.log 2> /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-mch17f71/task_outputs/fix-f6-recovery_event_failure/attempts/attempt_677ebadbb05be761dfb0/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-mch17f71/task_outputs/fix-f6-recovery_event_failure/attempts/attempt_677ebadbb05be761dfb0/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_677ebadbb05be761dfb0","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-mch17f71/task_outputs/fix-f6-recovery_event_failure/attempts/attempt_677ebadbb05be761dfb0/exit_status.json.tmp.$$'
mv '/tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-mch17f71/task_outputs/fix-f6-recovery_event_failure/attempts/attempt_677ebadbb05be761dfb0/exit_status.json.tmp.$$' /tmp/biomind-a1-unified-final-08/origin/scratch/biomind-plan-local-graph-mch17f71/task_outputs/fix-f6-recovery_event_failure/attempts/attempt_677ebadbb05be761dfb0/exit_status.json
exit "$rc"
