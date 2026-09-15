#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_9ff634addca4b90bcad7/stdout.log
: > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_9ff634addca4b90bcad7/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_9ff634addca4b90bcad7/approved.sh > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_9ff634addca4b90bcad7/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_9ff634addca4b90bcad7/approved.sh > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_9ff634addca4b90bcad7/stdout.log 2> /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_9ff634addca4b90bcad7/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_9ff634addca4b90bcad7/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_9ff634addca4b90bcad7","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_9ff634addca4b90bcad7/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_9ff634addca4b90bcad7/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-m1wtxz2b/task_outputs/fix-f6-6793_repeat/attempts/attempt_9ff634addca4b90bcad7/exit_status.json
exit "$rc"
