#!/bin/bash
set +e
: > /tmp/biomind-a1-final05-recovery-claim-01/scratch/biomind-plan-local-graph-5d70hlk1/task_outputs/fix-f6-recovery/attempts/attempt_90a0cf93c7b2a6455c7f/stdout.log
: > /tmp/biomind-a1-final05-recovery-claim-01/scratch/biomind-plan-local-graph-5d70hlk1/task_outputs/fix-f6-recovery/attempts/attempt_90a0cf93c7b2a6455c7f/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-a1-final05-recovery-claim-01/scratch/biomind-plan-local-graph-5d70hlk1/task_outputs/fix-f6-recovery/attempts/attempt_90a0cf93c7b2a6455c7f/approved.sh > /tmp/biomind-a1-final05-recovery-claim-01/scratch/biomind-plan-local-graph-5d70hlk1/task_outputs/fix-f6-recovery/attempts/attempt_90a0cf93c7b2a6455c7f/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-a1-final05-recovery-claim-01/scratch/biomind-plan-local-graph-5d70hlk1/task_outputs/fix-f6-recovery/attempts/attempt_90a0cf93c7b2a6455c7f/approved.sh > /tmp/biomind-a1-final05-recovery-claim-01/scratch/biomind-plan-local-graph-5d70hlk1/task_outputs/fix-f6-recovery/attempts/attempt_90a0cf93c7b2a6455c7f/stdout.log 2> /tmp/biomind-a1-final05-recovery-claim-01/scratch/biomind-plan-local-graph-5d70hlk1/task_outputs/fix-f6-recovery/attempts/attempt_90a0cf93c7b2a6455c7f/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-a1-final05-recovery-claim-01/scratch/biomind-plan-local-graph-5d70hlk1/task_outputs/fix-f6-recovery/attempts/attempt_90a0cf93c7b2a6455c7f/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_90a0cf93c7b2a6455c7f","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-a1-final05-recovery-claim-01/scratch/biomind-plan-local-graph-5d70hlk1/task_outputs/fix-f6-recovery/attempts/attempt_90a0cf93c7b2a6455c7f/exit_status.json.tmp.$$'
mv '/tmp/biomind-a1-final05-recovery-claim-01/scratch/biomind-plan-local-graph-5d70hlk1/task_outputs/fix-f6-recovery/attempts/attempt_90a0cf93c7b2a6455c7f/exit_status.json.tmp.$$' /tmp/biomind-a1-final05-recovery-claim-01/scratch/biomind-plan-local-graph-5d70hlk1/task_outputs/fix-f6-recovery/attempts/attempt_90a0cf93c7b2a6455c7f/exit_status.json
exit "$rc"
