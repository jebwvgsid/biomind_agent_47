#!/bin/bash
set +e
: > /tmp/biomind-a1-final06-recovery-claim-01/scratch/biomind-plan-local-graph-29402c32/task_outputs/fix-f6-recovery/attempts/attempt_574a18ab7094b788704e/stdout.log
: > /tmp/biomind-a1-final06-recovery-claim-01/scratch/biomind-plan-local-graph-29402c32/task_outputs/fix-f6-recovery/attempts/attempt_574a18ab7094b788704e/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-a1-final06-recovery-claim-01/scratch/biomind-plan-local-graph-29402c32/task_outputs/fix-f6-recovery/attempts/attempt_574a18ab7094b788704e/approved.sh > /tmp/biomind-a1-final06-recovery-claim-01/scratch/biomind-plan-local-graph-29402c32/task_outputs/fix-f6-recovery/attempts/attempt_574a18ab7094b788704e/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-a1-final06-recovery-claim-01/scratch/biomind-plan-local-graph-29402c32/task_outputs/fix-f6-recovery/attempts/attempt_574a18ab7094b788704e/approved.sh > /tmp/biomind-a1-final06-recovery-claim-01/scratch/biomind-plan-local-graph-29402c32/task_outputs/fix-f6-recovery/attempts/attempt_574a18ab7094b788704e/stdout.log 2> /tmp/biomind-a1-final06-recovery-claim-01/scratch/biomind-plan-local-graph-29402c32/task_outputs/fix-f6-recovery/attempts/attempt_574a18ab7094b788704e/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-a1-final06-recovery-claim-01/scratch/biomind-plan-local-graph-29402c32/task_outputs/fix-f6-recovery/attempts/attempt_574a18ab7094b788704e/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_574a18ab7094b788704e","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-a1-final06-recovery-claim-01/scratch/biomind-plan-local-graph-29402c32/task_outputs/fix-f6-recovery/attempts/attempt_574a18ab7094b788704e/exit_status.json.tmp.$$'
mv '/tmp/biomind-a1-final06-recovery-claim-01/scratch/biomind-plan-local-graph-29402c32/task_outputs/fix-f6-recovery/attempts/attempt_574a18ab7094b788704e/exit_status.json.tmp.$$' /tmp/biomind-a1-final06-recovery-claim-01/scratch/biomind-plan-local-graph-29402c32/task_outputs/fix-f6-recovery/attempts/attempt_574a18ab7094b788704e/exit_status.json
exit "$rc"
