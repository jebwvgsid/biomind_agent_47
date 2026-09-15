#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_03253494ca8d7ea6af6c/stdout.log
: > /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_03253494ca8d7ea6af6c/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_03253494ca8d7ea6af6c/approved.sh > /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_03253494ca8d7ea6af6c/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_03253494ca8d7ea6af6c/approved.sh > /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_03253494ca8d7ea6af6c/stdout.log 2> /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_03253494ca8d7ea6af6c/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_03253494ca8d7ea6af6c/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_03253494ca8d7ea6af6c","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_03253494ca8d7ea6af6c/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_03253494ca8d7ea6af6c/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_03253494ca8d7ea6af6c/exit_status.json
exit "$rc"
