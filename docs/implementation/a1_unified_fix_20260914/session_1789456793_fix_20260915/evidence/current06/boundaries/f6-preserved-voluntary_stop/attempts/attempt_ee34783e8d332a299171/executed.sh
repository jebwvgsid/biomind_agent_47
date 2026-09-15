#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current06/boundaries/scratch/biomind-plan-local-graph-kfxs5ywt/task_outputs/fix-f6-voluntary_stop/attempts/attempt_ee34783e8d332a299171/stdout.log
: > /tmp/biomind-6793-fix-20260915/current06/boundaries/scratch/biomind-plan-local-graph-kfxs5ywt/task_outputs/fix-f6-voluntary_stop/attempts/attempt_ee34783e8d332a299171/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current06/boundaries/scratch/biomind-plan-local-graph-kfxs5ywt/task_outputs/fix-f6-voluntary_stop/attempts/attempt_ee34783e8d332a299171/approved.sh > /tmp/biomind-6793-fix-20260915/current06/boundaries/scratch/biomind-plan-local-graph-kfxs5ywt/task_outputs/fix-f6-voluntary_stop/attempts/attempt_ee34783e8d332a299171/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current06/boundaries/scratch/biomind-plan-local-graph-kfxs5ywt/task_outputs/fix-f6-voluntary_stop/attempts/attempt_ee34783e8d332a299171/approved.sh > /tmp/biomind-6793-fix-20260915/current06/boundaries/scratch/biomind-plan-local-graph-kfxs5ywt/task_outputs/fix-f6-voluntary_stop/attempts/attempt_ee34783e8d332a299171/stdout.log 2> /tmp/biomind-6793-fix-20260915/current06/boundaries/scratch/biomind-plan-local-graph-kfxs5ywt/task_outputs/fix-f6-voluntary_stop/attempts/attempt_ee34783e8d332a299171/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current06/boundaries/scratch/biomind-plan-local-graph-kfxs5ywt/task_outputs/fix-f6-voluntary_stop/attempts/attempt_ee34783e8d332a299171/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_ee34783e8d332a299171","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current06/boundaries/scratch/biomind-plan-local-graph-kfxs5ywt/task_outputs/fix-f6-voluntary_stop/attempts/attempt_ee34783e8d332a299171/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current06/boundaries/scratch/biomind-plan-local-graph-kfxs5ywt/task_outputs/fix-f6-voluntary_stop/attempts/attempt_ee34783e8d332a299171/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current06/boundaries/scratch/biomind-plan-local-graph-kfxs5ywt/task_outputs/fix-f6-voluntary_stop/attempts/attempt_ee34783e8d332a299171/exit_status.json
exit "$rc"
