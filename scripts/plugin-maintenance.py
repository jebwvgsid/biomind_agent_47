#!/usr/bin/env python3
"""Explicit maintenance-window operations; never kills tasks or installs packages."""
import argparse
import json
from pathlib import Path
import sys
import subprocess
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_core import pi_plugin_adoption as adoption
from agent_core.pi_plugin_guard import GuardError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['entry-identity', 'audit-local', 'audit', 'fence', 'adopt', 'repair', 'rollback', 'restore-entries', 'activate', 'status'])
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--entry', type=Path)
    args = parser.parse_args()
    if args.action == 'entry-identity':
        if not args.entry: parser.error('--entry is required')
        value = adoption.entry_snapshot(args.entry)
        print(json.dumps({'path': str(args.entry.absolute()), 'kind': value['kind'], 'expectedSha256': value['sha256']}))
        return 0
    if not args.plan: parser.error('--plan is required')
    plan = adoption.load_plan(args.plan)
    if args.action == 'audit-local': result = adoption.audit_local(plan)
    elif args.action == 'audit':
        reports = adoption.audit_hosts(plan)
        result = adoption.audit_summary(plan, reports)
    elif args.action == 'fence': result = adoption.fence(plan)
    elif args.action in ('adopt', 'repair'): result = adoption.adopt(plan)
    elif args.action == 'rollback': result = adoption.rollback(plan)
    elif args.action in ('activate', 'restore-entries'): result = adoption.entries_action(plan, args.action == 'activate')
    else:
        path = Path(plan['stateDir']) / 'transaction.json'
        if path.exists():
            record = adoption.transaction(plan, path.parent)
            result = {'status': record['state'], 'id': record['id'], 'roots': record['roots'], 'planSha256': record['planSha256']}
        else: result = {'status': 'not-started'}
    print(json.dumps(result, ensure_ascii=False))
    return {'BLOCKED': 2, 'BUSY': 3}.get(result.get('status'), 0)


if __name__ == '__main__':
    try: raise SystemExit(main())
    except adoption.AuditFailure as error:
        print(json.dumps(error.result, ensure_ascii=False))
        raise SystemExit(2)
    except (GuardError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        is_audit = len(sys.argv) > 1 and sys.argv[1] in ('audit', 'audit-local')
        code = adoption.audit_error_code(error) if is_audit else str(error) if isinstance(error, GuardError) else 'ADOPTION_OPERATION_FAILED'
        print(json.dumps({'status': 'BUSY' if code in ('BUSY', 'LEGACY_OCCUPANCY_BUSY') else 'BLOCKED', 'code': code,
                          'recovery': 'Resolve the audit error and rerun; incomplete visibility is not proof of vacancy. No task is killed.' if is_audit else
                          'Inspect status; repair resumes a pending adoption, rollback restores only admission metadata. No task is killed.'}))
        raise SystemExit(3 if code in ('BUSY', 'LEGACY_OCCUPANCY_BUSY') else 2)
