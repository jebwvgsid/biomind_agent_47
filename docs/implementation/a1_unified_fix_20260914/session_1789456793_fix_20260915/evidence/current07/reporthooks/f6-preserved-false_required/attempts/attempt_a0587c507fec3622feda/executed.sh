#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-rmab200a/task_outputs/fix-f6-false_required/attempts/attempt_a0587c507fec3622feda/stdout.log
: > /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-rmab200a/task_outputs/fix-f6-false_required/attempts/attempt_a0587c507fec3622feda/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-rmab200a/task_outputs/fix-f6-false_required/attempts/attempt_a0587c507fec3622feda/approved.sh > /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-rmab200a/task_outputs/fix-f6-false_required/attempts/attempt_a0587c507fec3622feda/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-rmab200a/task_outputs/fix-f6-false_required/attempts/attempt_a0587c507fec3622feda/approved.sh > /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-rmab200a/task_outputs/fix-f6-false_required/attempts/attempt_a0587c507fec3622feda/stdout.log 2> /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-rmab200a/task_outputs/fix-f6-false_required/attempts/attempt_a0587c507fec3622feda/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-rmab200a/task_outputs/fix-f6-false_required/attempts/attempt_a0587c507fec3622feda/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_a0587c507fec3622feda","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-rmab200a/task_outputs/fix-f6-false_required/attempts/attempt_a0587c507fec3622feda/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-rmab200a/task_outputs/fix-f6-false_required/attempts/attempt_a0587c507fec3622feda/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-rmab200a/task_outputs/fix-f6-false_required/attempts/attempt_a0587c507fec3622feda/exit_status.json
exit "$rc"
