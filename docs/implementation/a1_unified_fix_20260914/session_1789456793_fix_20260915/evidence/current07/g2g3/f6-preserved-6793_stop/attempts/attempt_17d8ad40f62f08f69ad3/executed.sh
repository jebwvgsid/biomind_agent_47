#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_17d8ad40f62f08f69ad3/stdout.log
: > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_17d8ad40f62f08f69ad3/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_17d8ad40f62f08f69ad3/approved.sh > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_17d8ad40f62f08f69ad3/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_17d8ad40f62f08f69ad3/approved.sh > /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_17d8ad40f62f08f69ad3/stdout.log 2> /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_17d8ad40f62f08f69ad3/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_17d8ad40f62f08f69ad3/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_17d8ad40f62f08f69ad3","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_17d8ad40f62f08f69ad3/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_17d8ad40f62f08f69ad3/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current07/g2g3/scratch/biomind-plan-local-graph-r8hygtij/task_outputs/fix-f6-6793_stop/attempts/attempt_17d8ad40f62f08f69ad3/exit_status.json
exit "$rc"
