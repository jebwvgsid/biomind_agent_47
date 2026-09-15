#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_3607a4471f9ca39af30b/stdout.log
: > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_3607a4471f9ca39af30b/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_3607a4471f9ca39af30b/approved.sh > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_3607a4471f9ca39af30b/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_3607a4471f9ca39af30b/approved.sh > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_3607a4471f9ca39af30b/stdout.log 2> /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_3607a4471f9ca39af30b/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_3607a4471f9ca39af30b/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_3607a4471f9ca39af30b","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_3607a4471f9ca39af30b/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_3607a4471f9ca39af30b/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_3607a4471f9ca39af30b/exit_status.json
exit "$rc"
