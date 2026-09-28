"""Progress of a submitted step obligation, carried by existing work-result history."""
from copy import deepcopy
from agent_core.request_state import request_scope


def prior_results(state):
    task = state.get('task_state') or {}
    rows = [*(task.get('work_result_history') or []), task.get('pending_work_result') or {}]
    return [r for r in rows if r.get('request_scope') == request_scope(state)
            and r.get('step_id') == state.get('current_step_id')]


def progress_for(state, checked, event_id):
    rows = prior_results(state)
    prior = next((r.get('correction_progress') for r in reversed(rows) if r.get('correction_progress')), {})
    if prior.get('status') == 'resolved':
        prior = {}  # A closed obligation is not an unresolved future failure.
    progress = deepcopy(prior)
    # Exactly-once consumption is keyed by the durable submission event, not by
    # issue identity. A new call is a new failed trial, never a fresh allowance.
    replay = next((r for r in rows if r.get('submission_event') == event_id), None)
    if replay:
        return deepcopy(replay.get('correction_progress') or progress)
    progress.setdefault('failed_submissions', 0)
    progress.setdefault('seen_issues', [])
    progress.setdefault('confirmed_checks', [])
    if checked.get('passed'):
        # Only Commit seals this obligation. A changed file/version between
        # verification and Commit must not manufacture a fresh allowance.
        progress.update(status='validated_pending_commit', unresolved_issues=[])
        return progress
    progress['failed_submissions'] += 1
    progress['status'] = 'unresolved'
    issues = []
    for issue in checked.get('issues') or []:
        detail = issue.get('details') or {}
        parts = detail.get('problems') or [detail]
        for part in parts:
            # No call IDs, hashes, timestamps, or human messages in problem identity.
            key = ':'.join(str(x or '') for x in (issue.get('code'), detail.get('code'),
                part.get('code'), part.get('field'), part.get('key')))
            if key not in issues: issues.append(key)
    progress['unresolved_issues'] = issues[:32]
    progress['regressed_issues'] = [k for k in issues if k in progress['seen_issues'] and k not in prior.get('unresolved_issues', [])][:32]
    progress['seen_issues'] = list(dict.fromkeys([*progress['seen_issues'], *issues]))[:64]
    # Only actual check passes count as partial progress; not_checked is never a pass.
    import json
    passed = [json.dumps({'path':(c.get('file') or {}).get('path'), 'rule':c.get('rule')}, sort_keys=True)
              for c in checked.get('output_checks') or [] if c.get('passed') is True]
    progress['newly_confirmed_checks'] = [k for k in passed if k not in progress['confirmed_checks']][:32]
    progress['confirmed_checks'] = list(dict.fromkeys([*progress['confirmed_checks'], *passed]))[:64]
    return progress
