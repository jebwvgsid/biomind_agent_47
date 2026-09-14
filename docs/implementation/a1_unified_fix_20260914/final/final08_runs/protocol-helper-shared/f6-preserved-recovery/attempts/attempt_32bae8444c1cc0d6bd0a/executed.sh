#!/bin/bash
set +e
: > /tmp/biomind-a1-unified-final-08/protocol-helper-shared/scratch/biomind-plan-local-graph-6zpahwe7/task_outputs/fix-f6-recovery/attempts/attempt_32bae8444c1cc0d6bd0a/stdout.log
: > /tmp/biomind-a1-unified-final-08/protocol-helper-shared/scratch/biomind-plan-local-graph-6zpahwe7/task_outputs/fix-f6-recovery/attempts/attempt_32bae8444c1cc0d6bd0a/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-a1-unified-final-08/protocol-helper-shared/scratch/biomind-plan-local-graph-6zpahwe7/task_outputs/fix-f6-recovery/attempts/attempt_32bae8444c1cc0d6bd0a/approved.sh > /tmp/biomind-a1-unified-final-08/protocol-helper-shared/scratch/biomind-plan-local-graph-6zpahwe7/task_outputs/fix-f6-recovery/attempts/attempt_32bae8444c1cc0d6bd0a/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-a1-unified-final-08/protocol-helper-shared/scratch/biomind-plan-local-graph-6zpahwe7/task_outputs/fix-f6-recovery/attempts/attempt_32bae8444c1cc0d6bd0a/approved.sh > /tmp/biomind-a1-unified-final-08/protocol-helper-shared/scratch/biomind-plan-local-graph-6zpahwe7/task_outputs/fix-f6-recovery/attempts/attempt_32bae8444c1cc0d6bd0a/stdout.log 2> /tmp/biomind-a1-unified-final-08/protocol-helper-shared/scratch/biomind-plan-local-graph-6zpahwe7/task_outputs/fix-f6-recovery/attempts/attempt_32bae8444c1cc0d6bd0a/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-a1-unified-final-08/protocol-helper-shared/scratch/biomind-plan-local-graph-6zpahwe7/task_outputs/fix-f6-recovery/attempts/attempt_32bae8444c1cc0d6bd0a/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_32bae8444c1cc0d6bd0a","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-a1-unified-final-08/protocol-helper-shared/scratch/biomind-plan-local-graph-6zpahwe7/task_outputs/fix-f6-recovery/attempts/attempt_32bae8444c1cc0d6bd0a/exit_status.json.tmp.$$'
mv '/tmp/biomind-a1-unified-final-08/protocol-helper-shared/scratch/biomind-plan-local-graph-6zpahwe7/task_outputs/fix-f6-recovery/attempts/attempt_32bae8444c1cc0d6bd0a/exit_status.json.tmp.$$' /tmp/biomind-a1-unified-final-08/protocol-helper-shared/scratch/biomind-plan-local-graph-6zpahwe7/task_outputs/fix-f6-recovery/attempts/attempt_32bae8444c1cc0d6bd0a/exit_status.json
exit "$rc"
