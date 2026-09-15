#!/bin/bash
set +e
: > /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-yfg3vm7o/task_outputs/fix-f6-false_required/attempts/attempt_619a4c3500e423e7a3e3/stdout.log
: > /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-yfg3vm7o/task_outputs/fix-f6-false_required/attempts/attempt_619a4c3500e423e7a3e3/stderr.log
printf 'runner=%s script=%s\n' '/usr/bin/bash -o pipefail' /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-yfg3vm7o/task_outputs/fix-f6-false_required/attempts/attempt_619a4c3500e423e7a3e3/approved.sh > /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-yfg3vm7o/task_outputs/fix-f6-false_required/attempts/attempt_619a4c3500e423e7a3e3/command_trace.log
/usr/bin/bash -o pipefail /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-yfg3vm7o/task_outputs/fix-f6-false_required/attempts/attempt_619a4c3500e423e7a3e3/approved.sh > /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-yfg3vm7o/task_outputs/fix-f6-false_required/attempts/attempt_619a4c3500e423e7a3e3/stdout.log 2> /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-yfg3vm7o/task_outputs/fix-f6-false_required/attempts/attempt_619a4c3500e423e7a3e3/stderr.log
rc=$?
printf 'returncode=%s\n' "$rc" >> /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-yfg3vm7o/task_outputs/fix-f6-false_required/attempts/attempt_619a4c3500e423e7a3e3/command_trace.log
finished_at=$(date -Iseconds 2>/dev/null || date)
printf '{"attempt_id":"attempt_619a4c3500e423e7a3e3","returncode":%s,"finished_at":"%s"}\n' "$rc" "$finished_at" > '/tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-yfg3vm7o/task_outputs/fix-f6-false_required/attempts/attempt_619a4c3500e423e7a3e3/exit_status.json.tmp.$$'
mv '/tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-yfg3vm7o/task_outputs/fix-f6-false_required/attempts/attempt_619a4c3500e423e7a3e3/exit_status.json.tmp.$$' /tmp/biomind-6793-fix-20260915/current07/reporthooks/scratch/biomind-plan-local-graph-yfg3vm7o/task_outputs/fix-f6-false_required/attempts/attempt_619a4c3500e423e7a3e3/exit_status.json
exit "$rc"
