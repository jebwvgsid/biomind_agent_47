#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-6bo6g0p7/task_outputs/fix-f6-recovery/attempts/attempt_6225073b967e83b28849/stdout.log
: > /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-6bo6g0p7/task_outputs/fix-f6-recovery/attempts/attempt_6225073b967e83b28849/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-6bo6g0p7/task_outputs/fix-f6-recovery/attempts/attempt_6225073b967e83b28849/approved.sh > /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-6bo6g0p7/task_outputs/fix-f6-recovery/attempts/attempt_6225073b967e83b28849/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-6bo6g0p7/task_outputs/fix-f6-recovery/attempts/attempt_6225073b967e83b28849/approved.sh > /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-6bo6g0p7/task_outputs/fix-f6-recovery/attempts/attempt_6225073b967e83b28849/stdout.log 2> /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-6bo6g0p7/task_outputs/fix-f6-recovery/attempts/attempt_6225073b967e83b28849/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-6bo6g0p7/task_outputs/fix-f6-recovery/attempts/attempt_6225073b967e83b28849/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_6225073b967e83b28849","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-6bo6g0p7/task_outputs/fix-f6-recovery/attempts/attempt_6225073b967e83b28849/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-6bo6g0p7/task_outputs/fix-f6-recovery/attempts/attempt_6225073b967e83b28849/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-6bo6g0p7/task_outputs/fix-f6-recovery/attempts/attempt_6225073b967e83b28849/exit_status.json
exit "$rc"
