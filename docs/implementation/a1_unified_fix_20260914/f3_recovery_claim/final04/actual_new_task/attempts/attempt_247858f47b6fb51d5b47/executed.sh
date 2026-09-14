#!/bin/bash
set +e
: > /tmp/biomind-a1-final04-recovery-claim-01/scratch/biomind-plan-local-graph-o6d8aavp/task_outputs/fix-f6-recovery/attempts/attempt_247858f47b6fb51d5b47/stdout.log
: > /tmp/biomind-a1-final04-recovery-claim-01/scratch/biomind-plan-local-graph-o6d8aavp/task_outputs/fix-f6-recovery/attempts/attempt_247858f47b6fb51d5b47/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-a1-final04-recovery-claim-01/scratch/biomind-plan-local-graph-o6d8aavp/task_outputs/fix-f6-recovery/attempts/attempt_247858f47b6fb51d5b47/approved.sh > /tmp/biomind-a1-final04-recovery-claim-01/scratch/biomind-plan-local-graph-o6d8aavp/task_outputs/fix-f6-recovery/attempts/attempt_247858f47b6fb51d5b47/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-a1-final04-recovery-claim-01/scratch/biomind-plan-local-graph-o6d8aavp/task_outputs/fix-f6-recovery/attempts/attempt_247858f47b6fb51d5b47/approved.sh > /tmp/biomind-a1-final04-recovery-claim-01/scratch/biomind-plan-local-graph-o6d8aavp/task_outputs/fix-f6-recovery/attempts/attempt_247858f47b6fb51d5b47/stdout.log 2> /tmp/biomind-a1-final04-recovery-claim-01/scratch/biomind-plan-local-graph-o6d8aavp/task_outputs/fix-f6-recovery/attempts/attempt_247858f47b6fb51d5b47/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-a1-final04-recovery-claim-01/scratch/biomind-plan-local-graph-o6d8aavp/task_outputs/fix-f6-recovery/attempts/attempt_247858f47b6fb51d5b47/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_247858f47b6fb51d5b47","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-a1-final04-recovery-claim-01/scratch/biomind-plan-local-graph-o6d8aavp/task_outputs/fix-f6-recovery/attempts/attempt_247858f47b6fb51d5b47/exit_status.json.tmp.$$'
mv '/tmp/biomind-a1-final04-recovery-claim-01/scratch/biomind-plan-local-graph-o6d8aavp/task_outputs/fix-f6-recovery/attempts/attempt_247858f47b6fb51d5b47/exit_status.json.tmp.$$' /tmp/biomind-a1-final04-recovery-claim-01/scratch/biomind-plan-local-graph-o6d8aavp/task_outputs/fix-f6-recovery/attempts/attempt_247858f47b6fb51d5b47/exit_status.json
exit "$rc"
