#!/bin/bash
set +e
: > /tmp/biomind-a1-recovery-claim-01/results/scratch/biomind-plan-local-graph-9vdtth0z/task_outputs/fix-f6-recovery/attempts/attempt_228b26354d14b2b10a59/stdout.log
: > /tmp/biomind-a1-recovery-claim-01/results/scratch/biomind-plan-local-graph-9vdtth0z/task_outputs/fix-f6-recovery/attempts/attempt_228b26354d14b2b10a59/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-a1-recovery-claim-01/results/scratch/biomind-plan-local-graph-9vdtth0z/task_outputs/fix-f6-recovery/attempts/attempt_228b26354d14b2b10a59/approved.sh > /tmp/biomind-a1-recovery-claim-01/results/scratch/biomind-plan-local-graph-9vdtth0z/task_outputs/fix-f6-recovery/attempts/attempt_228b26354d14b2b10a59/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-a1-recovery-claim-01/results/scratch/biomind-plan-local-graph-9vdtth0z/task_outputs/fix-f6-recovery/attempts/attempt_228b26354d14b2b10a59/approved.sh > /tmp/biomind-a1-recovery-claim-01/results/scratch/biomind-plan-local-graph-9vdtth0z/task_outputs/fix-f6-recovery/attempts/attempt_228b26354d14b2b10a59/stdout.log 2> /tmp/biomind-a1-recovery-claim-01/results/scratch/biomind-plan-local-graph-9vdtth0z/task_outputs/fix-f6-recovery/attempts/attempt_228b26354d14b2b10a59/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-a1-recovery-claim-01/results/scratch/biomind-plan-local-graph-9vdtth0z/task_outputs/fix-f6-recovery/attempts/attempt_228b26354d14b2b10a59/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_228b26354d14b2b10a59","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-a1-recovery-claim-01/results/scratch/biomind-plan-local-graph-9vdtth0z/task_outputs/fix-f6-recovery/attempts/attempt_228b26354d14b2b10a59/exit_status.json.tmp.$$'
mv '/tmp/biomind-a1-recovery-claim-01/results/scratch/biomind-plan-local-graph-9vdtth0z/task_outputs/fix-f6-recovery/attempts/attempt_228b26354d14b2b10a59/exit_status.json.tmp.$$' /tmp/biomind-a1-recovery-claim-01/results/scratch/biomind-plan-local-graph-9vdtth0z/task_outputs/fix-f6-recovery/attempts/attempt_228b26354d14b2b10a59/exit_status.json
exit "$rc"
