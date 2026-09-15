#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-8ju_03y0/task_outputs/fix-f6-false_diagnostic/attempts/attempt_c88a36693ed9603bc850/stdout.log
: > /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-8ju_03y0/task_outputs/fix-f6-false_diagnostic/attempts/attempt_c88a36693ed9603bc850/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-8ju_03y0/task_outputs/fix-f6-false_diagnostic/attempts/attempt_c88a36693ed9603bc850/approved.sh > /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-8ju_03y0/task_outputs/fix-f6-false_diagnostic/attempts/attempt_c88a36693ed9603bc850/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-8ju_03y0/task_outputs/fix-f6-false_diagnostic/attempts/attempt_c88a36693ed9603bc850/approved.sh > /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-8ju_03y0/task_outputs/fix-f6-false_diagnostic/attempts/attempt_c88a36693ed9603bc850/stdout.log 2> /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-8ju_03y0/task_outputs/fix-f6-false_diagnostic/attempts/attempt_c88a36693ed9603bc850/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-8ju_03y0/task_outputs/fix-f6-false_diagnostic/attempts/attempt_c88a36693ed9603bc850/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_c88a36693ed9603bc850","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-8ju_03y0/task_outputs/fix-f6-false_diagnostic/attempts/attempt_c88a36693ed9603bc850/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-8ju_03y0/task_outputs/fix-f6-false_diagnostic/attempts/attempt_c88a36693ed9603bc850/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current07/f2approval/scratch/biomind-plan-local-graph-8ju_03y0/task_outputs/fix-f6-false_diagnostic/attempts/attempt_c88a36693ed9603bc850/exit_status.json
exit "$rc"
