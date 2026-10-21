#!/usr/bin/env bash
# Finish the already authorized Studio rollout from a real host terminal.
set -euo pipefail
biomind_deploy_python='/public/home/liusiqi/anaconda3/envs/biomind/bin/python'
biomind_deploy_root='/public/home/liusiqi/03.毕设/BioMind_Production_LongTask_20261021'
biomind_deploy_worktree='/public/home/liusiqi/03.毕设/BioMind-worktrees/submission-production-20261020'
if ! git -C "$biomind_deploy_worktree" diff --cached --quiet; then
    printf '%s\n' 'Index has staged changes; finish or unstage them before committing this deployment record.' >&2
    exit 2
fi
"$biomind_deploy_python" "$biomind_deploy_root/scripts/deploy_studio.py" --apply
"$biomind_deploy_python" "$biomind_deploy_root/scripts/verify_studio.py"
"$biomind_deploy_python" "$biomind_deploy_root/scripts/finalize.py"
git -C "$biomind_deploy_worktree" add -- docs/implementation/long-task-production-20261021
if ! git -C "$biomind_deploy_worktree" diff --cached --quiet; then
    git -C "$biomind_deploy_worktree" commit -m 'deploy: verify long-task core in production Studio'
fi
