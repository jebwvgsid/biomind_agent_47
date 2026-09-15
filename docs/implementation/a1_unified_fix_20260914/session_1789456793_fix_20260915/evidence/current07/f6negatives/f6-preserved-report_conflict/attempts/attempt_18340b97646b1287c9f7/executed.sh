#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-0200ixts/task_outputs/fix-f6-report_conflict/attempts/attempt_18340b97646b1287c9f7/stdout.log
: > /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-0200ixts/task_outputs/fix-f6-report_conflict/attempts/attempt_18340b97646b1287c9f7/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-0200ixts/task_outputs/fix-f6-report_conflict/attempts/attempt_18340b97646b1287c9f7/approved.sh > /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-0200ixts/task_outputs/fix-f6-report_conflict/attempts/attempt_18340b97646b1287c9f7/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-0200ixts/task_outputs/fix-f6-report_conflict/attempts/attempt_18340b97646b1287c9f7/approved.sh > /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-0200ixts/task_outputs/fix-f6-report_conflict/attempts/attempt_18340b97646b1287c9f7/stdout.log 2> /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-0200ixts/task_outputs/fix-f6-report_conflict/attempts/attempt_18340b97646b1287c9f7/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-0200ixts/task_outputs/fix-f6-report_conflict/attempts/attempt_18340b97646b1287c9f7/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_18340b97646b1287c9f7","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-0200ixts/task_outputs/fix-f6-report_conflict/attempts/attempt_18340b97646b1287c9f7/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-0200ixts/task_outputs/fix-f6-report_conflict/attempts/attempt_18340b97646b1287c9f7/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-0200ixts/task_outputs/fix-f6-report_conflict/attempts/attempt_18340b97646b1287c9f7/exit_status.json
exit "$rc"
