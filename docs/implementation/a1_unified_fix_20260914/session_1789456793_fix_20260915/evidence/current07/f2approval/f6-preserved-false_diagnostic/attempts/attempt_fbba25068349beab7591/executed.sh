#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-xpd178rl/task_outputs/fix-f6-false_diagnostic/attempts/attempt_fbba25068349beab7591/stdout.log
: > /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-xpd178rl/task_outputs/fix-f6-false_diagnostic/attempts/attempt_fbba25068349beab7591/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-xpd178rl/task_outputs/fix-f6-false_diagnostic/attempts/attempt_fbba25068349beab7591/approved.sh > /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-xpd178rl/task_outputs/fix-f6-false_diagnostic/attempts/attempt_fbba25068349beab7591/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-xpd178rl/task_outputs/fix-f6-false_diagnostic/attempts/attempt_fbba25068349beab7591/approved.sh > /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-xpd178rl/task_outputs/fix-f6-false_diagnostic/attempts/attempt_fbba25068349beab7591/stdout.log 2> /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-xpd178rl/task_outputs/fix-f6-false_diagnostic/attempts/attempt_fbba25068349beab7591/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-xpd178rl/task_outputs/fix-f6-false_diagnostic/attempts/attempt_fbba25068349beab7591/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_fbba25068349beab7591","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-xpd178rl/task_outputs/fix-f6-false_diagnostic/attempts/attempt_fbba25068349beab7591/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-xpd178rl/task_outputs/fix-f6-false_diagnostic/attempts/attempt_fbba25068349beab7591/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-xpd178rl/task_outputs/fix-f6-false_diagnostic/attempts/attempt_fbba25068349beab7591/exit_status.json
exit "$rc"
