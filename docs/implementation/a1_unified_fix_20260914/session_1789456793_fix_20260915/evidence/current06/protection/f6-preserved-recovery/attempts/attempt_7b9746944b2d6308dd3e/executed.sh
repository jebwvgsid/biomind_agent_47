#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-jveqcgep/task_outputs/fix-f6-recovery/attempts/attempt_7b9746944b2d6308dd3e/stdout.log
: > /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-jveqcgep/task_outputs/fix-f6-recovery/attempts/attempt_7b9746944b2d6308dd3e/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-jveqcgep/task_outputs/fix-f6-recovery/attempts/attempt_7b9746944b2d6308dd3e/approved.sh > /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-jveqcgep/task_outputs/fix-f6-recovery/attempts/attempt_7b9746944b2d6308dd3e/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-jveqcgep/task_outputs/fix-f6-recovery/attempts/attempt_7b9746944b2d6308dd3e/approved.sh > /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-jveqcgep/task_outputs/fix-f6-recovery/attempts/attempt_7b9746944b2d6308dd3e/stdout.log 2> /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-jveqcgep/task_outputs/fix-f6-recovery/attempts/attempt_7b9746944b2d6308dd3e/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-jveqcgep/task_outputs/fix-f6-recovery/attempts/attempt_7b9746944b2d6308dd3e/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_7b9746944b2d6308dd3e","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-jveqcgep/task_outputs/fix-f6-recovery/attempts/attempt_7b9746944b2d6308dd3e/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-jveqcgep/task_outputs/fix-f6-recovery/attempts/attempt_7b9746944b2d6308dd3e/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current06/protection/scratch/biomind-plan-local-graph-jveqcgep/task_outputs/fix-f6-recovery/attempts/attempt_7b9746944b2d6308dd3e/exit_status.json
exit "$rc"
