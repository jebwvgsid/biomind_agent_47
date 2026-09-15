#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-vk03pp64/task_outputs/fix-f6-protocol-blocked/attempts/attempt_6e6cc99c701c86d2f419/stdout.log
: > /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-vk03pp64/task_outputs/fix-f6-protocol-blocked/attempts/attempt_6e6cc99c701c86d2f419/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-vk03pp64/task_outputs/fix-f6-protocol-blocked/attempts/attempt_6e6cc99c701c86d2f419/approved.sh > /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-vk03pp64/task_outputs/fix-f6-protocol-blocked/attempts/attempt_6e6cc99c701c86d2f419/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-vk03pp64/task_outputs/fix-f6-protocol-blocked/attempts/attempt_6e6cc99c701c86d2f419/approved.sh > /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-vk03pp64/task_outputs/fix-f6-protocol-blocked/attempts/attempt_6e6cc99c701c86d2f419/stdout.log 2> /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-vk03pp64/task_outputs/fix-f6-protocol-blocked/attempts/attempt_6e6cc99c701c86d2f419/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-vk03pp64/task_outputs/fix-f6-protocol-blocked/attempts/attempt_6e6cc99c701c86d2f419/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_6e6cc99c701c86d2f419","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-vk03pp64/task_outputs/fix-f6-protocol-blocked/attempts/attempt_6e6cc99c701c86d2f419/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-vk03pp64/task_outputs/fix-f6-protocol-blocked/attempts/attempt_6e6cc99c701c86d2f419/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-vk03pp64/task_outputs/fix-f6-protocol-blocked/attempts/attempt_6e6cc99c701c86d2f419/exit_status.json
exit "$rc"
