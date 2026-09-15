#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_9fb71bd76971c26fdabc/stdout.log
: > /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_9fb71bd76971c26fdabc/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_9fb71bd76971c26fdabc/approved.sh > /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_9fb71bd76971c26fdabc/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_9fb71bd76971c26fdabc/approved.sh > /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_9fb71bd76971c26fdabc/stdout.log 2> /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_9fb71bd76971c26fdabc/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_9fb71bd76971c26fdabc/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_9fb71bd76971c26fdabc","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_9fb71bd76971c26fdabc/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_9fb71bd76971c26fdabc/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current07/lastchance/scratch/biomind-plan-local-graph-k1o6x2b4/task_outputs/fix-f6-6793_last_chance/attempts/attempt_9fb71bd76971c26fdabc/exit_status.json
exit "$rc"
