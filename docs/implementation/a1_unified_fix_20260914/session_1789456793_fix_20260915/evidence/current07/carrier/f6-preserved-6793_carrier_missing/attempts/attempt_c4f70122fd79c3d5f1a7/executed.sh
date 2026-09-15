#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_c4f70122fd79c3d5f1a7/stdout.log
: > /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_c4f70122fd79c3d5f1a7/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_c4f70122fd79c3d5f1a7/approved.sh > /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_c4f70122fd79c3d5f1a7/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_c4f70122fd79c3d5f1a7/approved.sh > /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_c4f70122fd79c3d5f1a7/stdout.log 2> /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_c4f70122fd79c3d5f1a7/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_c4f70122fd79c3d5f1a7/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_c4f70122fd79c3d5f1a7","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_c4f70122fd79c3d5f1a7/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_c4f70122fd79c3d5f1a7/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current07/carrier/scratch/biomind-plan-local-graph-t3367gy5/task_outputs/fix-f6-6793_carrier_missing/attempts/attempt_c4f70122fd79c3d5f1a7/exit_status.json
exit "$rc"
