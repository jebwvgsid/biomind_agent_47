#!/bin/bash
set +e
: > /tmp/biomind-a1-unified-final-06/protocol-helper-shared/scratch/biomind-plan-local-graph-mdz34sea/task_outputs/fix-f6-recovery/attempts/attempt_498692c1275c31a58489/stdout.log
: > /tmp/biomind-a1-unified-final-06/protocol-helper-shared/scratch/biomind-plan-local-graph-mdz34sea/task_outputs/fix-f6-recovery/attempts/attempt_498692c1275c31a58489/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-a1-unified-final-06/protocol-helper-shared/scratch/biomind-plan-local-graph-mdz34sea/task_outputs/fix-f6-recovery/attempts/attempt_498692c1275c31a58489/approved.sh > /tmp/biomind-a1-unified-final-06/protocol-helper-shared/scratch/biomind-plan-local-graph-mdz34sea/task_outputs/fix-f6-recovery/attempts/attempt_498692c1275c31a58489/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-a1-unified-final-06/protocol-helper-shared/scratch/biomind-plan-local-graph-mdz34sea/task_outputs/fix-f6-recovery/attempts/attempt_498692c1275c31a58489/approved.sh > /tmp/biomind-a1-unified-final-06/protocol-helper-shared/scratch/biomind-plan-local-graph-mdz34sea/task_outputs/fix-f6-recovery/attempts/attempt_498692c1275c31a58489/stdout.log 2> /tmp/biomind-a1-unified-final-06/protocol-helper-shared/scratch/biomind-plan-local-graph-mdz34sea/task_outputs/fix-f6-recovery/attempts/attempt_498692c1275c31a58489/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-a1-unified-final-06/protocol-helper-shared/scratch/biomind-plan-local-graph-mdz34sea/task_outputs/fix-f6-recovery/attempts/attempt_498692c1275c31a58489/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_498692c1275c31a58489","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-a1-unified-final-06/protocol-helper-shared/scratch/biomind-plan-local-graph-mdz34sea/task_outputs/fix-f6-recovery/attempts/attempt_498692c1275c31a58489/exit_status.json.tmp.$$'
mv '/tmp/biomind-a1-unified-final-06/protocol-helper-shared/scratch/biomind-plan-local-graph-mdz34sea/task_outputs/fix-f6-recovery/attempts/attempt_498692c1275c31a58489/exit_status.json.tmp.$$' /tmp/biomind-a1-unified-final-06/protocol-helper-shared/scratch/biomind-plan-local-graph-mdz34sea/task_outputs/fix-f6-recovery/attempts/attempt_498692c1275c31a58489/exit_status.json
exit "$rc"
