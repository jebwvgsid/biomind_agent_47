#!/usr/bin/env python3
"""Read-only review: explain blockers and inspect Slurm-discovered hosts.

Produces evidence, never an approved adoption plan. Does not read credentials,
submit/cancel jobs, stop processes, mark resources, or change launchers.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_core import pi_plugin_adoption as adoption


def collect_audit(plan):
    try:
        return adoption.audit_summary(plan, adoption.audit_hosts(plan))
    except adoption.AuditFailure as error:
        return error.result


def slurm_fields(text):
    # Slurm -o separates attributes with whitespace; values may contain spaces.
    # Keep only nonsecret identity/entry paths, never raw stdout or job scripts.
    matches = list(re.finditer(r'(?:^|\s)([A-Za-z][A-Za-z0-9_]*)=', text))
    raw = {m[1]: text[m.end():matches[i+1].start() if i+1<len(matches) else len(text)].strip()
           for i,m in enumerate(matches)}
    result = {}
    for key in ('JobId', 'JobState', 'UserId', 'NodeList', 'BatchHost'):
        value = raw.get(key)
        if value and len(value) < 2048 and not any(c in value for c in '\r\n\0'):
            result[key] = value
    for key in ('Command', 'WorkDir'):
        value = raw.get(key, '')
        if value.startswith('/') and len(value) <= 4096 and not any(c in value for c in '\r\n\0'):
            # Command can be shell text: print a path only when it is a file.
            if key == 'WorkDir' or Path(value).is_file(): result[key] = value
            elif value: result['CommandSha256'] = hashlib.sha256(value.encode()).hexdigest()
    return result


def control(executable, *args):
    response = subprocess.run([executable, *args], capture_output=True, text=True, timeout=20)
    if response.returncode: raise ValueError('SCONTROL_QUERY_FAILED')
    if len(response.stdout) > 131072: raise ValueError('SCONTROL_RESPONSE_TOO_LARGE')
    return response.stdout


def job_review(audit, executable):
    jobs = {row['jobId']: row for host in audit.get('reports', []) for row in host.get('slurmJobs', [])}
    nodes, details, failures = set(), [], []
    for identifier, row in sorted(jobs.items()):
        detail = {**row}
        try:
            if not re.fullmatch(r'[0-9_+]{1,128}', identifier): raise ValueError()
            detail['metadata'] = slurm_fields(control(executable, '-o', 'show', 'job', identifier))
            # Mismatched or missing job metadata is a failure, not vacancy.
            if detail['metadata'].get('JobId') != identifier: raise ValueError()
        except (OSError, ValueError, subprocess.SubprocessError):
            detail['metadataStatus'] = 'BLOCKED'
            failures.append({'jobId': identifier, 'code': 'JOB_METADATA_UNAVAILABLE'})
        else:
            detail['metadataStatus'] = 'PASS'
        expression = row.get('nodeList', '')
        if expression and expression not in ('(null)', 'None', 'N/A'):
            try:
                found = control(executable, 'show', 'hostnames', expression).splitlines()
                if not found or len(found) > 32 or any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', n) for n in found):
                    raise ValueError()
                nodes.update(found)
                detail['discoveredHosts'] = found
            except (OSError, ValueError, subprocess.SubprocessError):
                failures.append({'jobId': identifier, 'code': 'JOB_HOST_DISCOVERY_FAILED'})
        else:
            detail['discoveredHosts'] = []
        details.append(detail)
    if len(nodes) > 32: raise ValueError('TOO_MANY_COMPUTE_HOSTS')
    return {'jobs': details, 'hosts': sorted(nodes), 'failures': failures}


def review_plan(source, hosts):
    plan = {k:v for k,v in source.items() if not k.startswith('_')}
    plan['hosts'] = list(plan['hosts'])
    known = {h['name'] for h in plan['hosts']}
    for host in hosts:
        if host not in known:
            plan['hosts'].append({'name': host, 'ssh': host, 'reviewedUnrelatedProcesses': []})
            known.add(host)
    if len(plan['hosts']) > 32: raise ValueError('TOO_MANY_REVIEW_HOSTS')
    for flag in ('hostInventoryConfirmed', 'entryInventoryConfirmed', 'maintenanceApproved'):
        plan[flag] = False
    return plan


def host_summary(report):
    """Absent observations are unknown, not empty process inventories."""
    checks = report.get('checks', {})
    process_checked = checks.get('processes') in ('PASS', 'BLOCKED')
    writers_checked = checks.get('writers') in ('PASS', 'BLOCKED')
    def count(field, checked):
        rows = report.get(field)
        return len(rows) if checked and isinstance(rows, list) else None
    return {'host': report['host'], 'status': report['status'], 'code': report.get('code'),
            'checks': checks, 'stage': report.get('stage'),
            'legacyReferences': count('legacyProcesses', process_checked),
            'unknownProcesses': count('unreviewedProcesses', process_checked),
            'unreadableProcesses': count('unreadablePids', process_checked),
            'activeWriters': count('activeWriterClaims', writers_checked)}


def job_host_coverage(hosts, declared, audit):
    """Requested SSH targets and complete host observations are different sets."""
    required = set(hosts)
    reports = {r['host']: r for r in audit.get('reports', [])}
    complete = set()
    for host, report in reports.items():
        checks = report.get('checks', {})
        if (report.get('status') in ('clear', 'BUSY') and
                all(checks.get(k) == 'PASS' for k in ('hostNamespace', 'processes', 'writers')) and
                checks.get('scheduler') in ('PASS', 'NOT_APPLICABLE')):
            complete.add(host)
    return {'jobHostsNotDeclared': sorted(required-set(declared)),
            'jobHostsAttempted': sorted(required & reports.keys()),
            'jobHostsVerified': sorted(required & complete),
            'jobHostsNotYetAudited': sorted(required-complete),
            'jobHostCoverageComplete': required <= complete}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--output-directory', required=True)
    args = parser.parse_args()
    if Path('/proc/1/comm').read_text().strip() not in ('systemd', 'init'):
        print(json.dumps({'status': 'BLOCKED', 'code': 'ADOPTION_HOST_PID_NAMESPACE_REQUIRED'}));return 2
    # When frozen, verify every packaged implementation before invoking SSH.
    manifest = Path(__file__).resolve().parents[1] / 'TOOLS.json'
    if manifest.exists():
        for item in json.loads(manifest.read_text())['files']:
            if hashlib.sha256((manifest.parent/item['path']).read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('MAINTENANCE_TOOL_CONTENT_MISMATCH')
    directory = Path(args.output_directory)
    if not directory.is_dir() or directory.stat().st_uid != os.getuid(): raise ValueError('INVALID_EVIDENCE_DIRECTORY')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    original = adoption.load_plan(args.plan)
    print('Collecting fresh process references and scheduler identity (read only)...', flush=True)
    initial = collect_audit(original)
    initial_file = directory / ('review-initial-'+stamp+'.json')
    adoption.save(initial_file, initial)
    scheduler = Path(original['schedulerExecutable']).parent / 'scontrol'
    discovered = job_review(initial, str(scheduler))
    expanded_path = directory / ('review-plan-'+stamp+'.json')
    adoption.save(expanded_path, review_plan(original, discovered['hosts']))
    expanded = adoption.load_plan(expanded_path)
    print('Inspecting declared and discovered hosts: '+', '.join(h['name'] for h in expanded['hosts']), flush=True)
    audit = collect_audit(expanded)
    final_jobs = job_review(audit, str(scheduler))
    coverage = job_host_coverage(final_jobs['hosts'], [h['name'] for h in expanded['hosts']], audit)
    report = {'schema':'BioMindMaintenanceBlockerReviewV1','status':'REVIEW_REQUIRED',
              'atUTC':datetime.datetime.now(datetime.timezone.utc).isoformat(), 'originHost':socket.gethostname(),
              'originPid1':Path('/proc/1/comm').read_text().strip(), 'sourcePlan':str(args.plan),
              'sourcePlanSha256':original['_sha256'], 'reviewPlan':str(expanded_path),
              'initialAudit':str(initial_file), 'jobDiscovery':discovered, 'latestJobReview':final_jobs,
              **coverage, 'audit':audit,
              'productionChanged':False,'processesStopped':False,'inventoryApproved':False,
              'resourceAdopted':False,'formalSwitch':False}
    output = directory / ('blocker-review-'+stamp+'.json')
    adoption.save(output, report)
    print(json.dumps({'status':'REVIEW_REQUIRED','hostAuditStatus':audit['status'],
        'hosts':[host_summary(r) for r in audit['reports']],
        'jobDiscoveryFailures':discovered['failures']+final_jobs['failures'],
        **coverage, 'report':str(output),'productionChanged':False},ensure_ascii=False),flush=True)
    return 2 if audit['status']=='BLOCKED' or discovered['failures'] or final_jobs['failures'] or not coverage['jobHostCoverageComplete'] else 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (OSError, ValueError, KeyError, adoption.guard.GuardError, subprocess.SubprocessError):
        print(json.dumps({'status':'BLOCKED','code':'MAINTENANCE_REVIEW_FAILED','productionChanged':False}),flush=True)
        raise SystemExit(2)
