#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_28f622a2be49cf78ad1b/stdout.log
: > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_28f622a2be49cf78ad1b/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_28f622a2be49cf78ad1b/approved.sh > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_28f622a2be49cf78ad1b/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_28f622a2be49cf78ad1b/approved.sh > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_28f622a2be49cf78ad1b/stdout.log 2> /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_28f622a2be49cf78ad1b/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_28f622a2be49cf78ad1b/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_28f622a2be49cf78ad1b","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_28f622a2be49cf78ad1b/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_28f622a2be49cf78ad1b/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_28f622a2be49cf78ad1b/exit_status.json
exit "$rc"
