#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_7d861e896c71cb54d0a1/stdout.log
: > /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_7d861e896c71cb54d0a1/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_7d861e896c71cb54d0a1/approved.sh > /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_7d861e896c71cb54d0a1/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_7d861e896c71cb54d0a1/approved.sh > /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_7d861e896c71cb54d0a1/stdout.log 2> /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_7d861e896c71cb54d0a1/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_7d861e896c71cb54d0a1/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_7d861e896c71cb54d0a1","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_7d861e896c71cb54d0a1/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_7d861e896c71cb54d0a1/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_7d861e896c71cb54d0a1/exit_status.json
exit "$rc"
