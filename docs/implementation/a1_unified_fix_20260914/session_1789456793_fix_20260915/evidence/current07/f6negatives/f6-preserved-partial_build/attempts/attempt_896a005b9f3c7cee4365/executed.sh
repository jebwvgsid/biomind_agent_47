#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-xus_4zrw/task_outputs/fix-f6-partial_build/attempts/attempt_896a005b9f3c7cee4365/stdout.log
: > /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-xus_4zrw/task_outputs/fix-f6-partial_build/attempts/attempt_896a005b9f3c7cee4365/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-xus_4zrw/task_outputs/fix-f6-partial_build/attempts/attempt_896a005b9f3c7cee4365/approved.sh > /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-xus_4zrw/task_outputs/fix-f6-partial_build/attempts/attempt_896a005b9f3c7cee4365/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-xus_4zrw/task_outputs/fix-f6-partial_build/attempts/attempt_896a005b9f3c7cee4365/approved.sh > /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-xus_4zrw/task_outputs/fix-f6-partial_build/attempts/attempt_896a005b9f3c7cee4365/stdout.log 2> /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-xus_4zrw/task_outputs/fix-f6-partial_build/attempts/attempt_896a005b9f3c7cee4365/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-xus_4zrw/task_outputs/fix-f6-partial_build/attempts/attempt_896a005b9f3c7cee4365/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_896a005b9f3c7cee4365","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-xus_4zrw/task_outputs/fix-f6-partial_build/attempts/attempt_896a005b9f3c7cee4365/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-xus_4zrw/task_outputs/fix-f6-partial_build/attempts/attempt_896a005b9f3c7cee4365/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-xus_4zrw/task_outputs/fix-f6-partial_build/attempts/attempt_896a005b9f3c7cee4365/exit_status.json
exit "$rc"
