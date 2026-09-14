#!/bin/bash
set +e
: > /tmp/biomind-a1-final08-recovery-claim-01/scratch/biomind-plan-local-graph-ua8i2cd7/task_outputs/fix-f6-recovery/attempts/attempt_8246b13b167af4ea4b00/stdout.log
: > /tmp/biomind-a1-final08-recovery-claim-01/scratch/biomind-plan-local-graph-ua8i2cd7/task_outputs/fix-f6-recovery/attempts/attempt_8246b13b167af4ea4b00/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-a1-final08-recovery-claim-01/scratch/biomind-plan-local-graph-ua8i2cd7/task_outputs/fix-f6-recovery/attempts/attempt_8246b13b167af4ea4b00/approved.sh > /tmp/biomind-a1-final08-recovery-claim-01/scratch/biomind-plan-local-graph-ua8i2cd7/task_outputs/fix-f6-recovery/attempts/attempt_8246b13b167af4ea4b00/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-a1-final08-recovery-claim-01/scratch/biomind-plan-local-graph-ua8i2cd7/task_outputs/fix-f6-recovery/attempts/attempt_8246b13b167af4ea4b00/approved.sh > /tmp/biomind-a1-final08-recovery-claim-01/scratch/biomind-plan-local-graph-ua8i2cd7/task_outputs/fix-f6-recovery/attempts/attempt_8246b13b167af4ea4b00/stdout.log 2> /tmp/biomind-a1-final08-recovery-claim-01/scratch/biomind-plan-local-graph-ua8i2cd7/task_outputs/fix-f6-recovery/attempts/attempt_8246b13b167af4ea4b00/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-a1-final08-recovery-claim-01/scratch/biomind-plan-local-graph-ua8i2cd7/task_outputs/fix-f6-recovery/attempts/attempt_8246b13b167af4ea4b00/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_8246b13b167af4ea4b00","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-a1-final08-recovery-claim-01/scratch/biomind-plan-local-graph-ua8i2cd7/task_outputs/fix-f6-recovery/attempts/attempt_8246b13b167af4ea4b00/exit_status.json.tmp.$$'
mv '/tmp/biomind-a1-final08-recovery-claim-01/scratch/biomind-plan-local-graph-ua8i2cd7/task_outputs/fix-f6-recovery/attempts/attempt_8246b13b167af4ea4b00/exit_status.json.tmp.$$' /tmp/biomind-a1-final08-recovery-claim-01/scratch/biomind-plan-local-graph-ua8i2cd7/task_outputs/fix-f6-recovery/attempts/attempt_8246b13b167af4ea4b00/exit_status.json
exit "$rc"
