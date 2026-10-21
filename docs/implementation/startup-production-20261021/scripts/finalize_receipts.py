"""Assemble a deployment record only after all real-entry acceptance passes."""
from pathlib import Path
import datetime
import hashlib
import json
import shutil

B = Path('/public/home/liusiqi/03.毕设')
O = B / 'BioMind_Startup_Production_20261021'
W = B / 'BioMind-worktrees/submission-production-20261020'
S = B / 'BioMind-worktrees/studio-startup-20261021'
D = W / 'docs/implementation/startup-production-20261021'


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    build = read(O / 'build-result.json')
    backend = read(O / 'backend-result.json')
    core = read(Path(build['root']) / 'RELEASE.json')
    cli = read(O / 'cli-deployment.json')
    studio = read(O / 'studio-deployment.json')
    owner = read(O / 'studio-owner-verification.json')
    after = read(O / 'evidence/production-after.json')
    before = read(O / 'evidence/production-before.json')
    audit = read(O / 'evidence/audit-summary.json')
    tested = read(O / 'evidence/tested-artifacts.json')
    for key in ('coreValidation', 'studioValidation'):
        assert audit[key]['status'] == 'PASS'
        assert audit[key]['failed'] == audit[key]['skipped'] == 0
        for row in audit[key]['receipts']:
            assert sha(Path(row['path'])) == row['sha256']
    assert audit['coreValidation']['candidateManifestBuildId'] == tested['buildId'] == build['releaseId']
    assert tested['status'] == 'PASS'
    assert audit['sourceCommits']['pc1IntegratedAndCandidateCore'] == tested['sourceCommit'] == core['sourceCommit']
    assert audit['sourceCommits']['studioBackendCandidate'] == backend['sourceCommit']
    assert audit['studioValidation']['build']['buildId'] == backend['buildId']
    checks = [read(O / 'evidence' / name / 'receipt.json')
              for name in ('official-cli', 'project-cli')]
    assert build['result'] == backend['status'] == owner['status'] == 'PASS'
    assert cli['status'] == studio['status'] == 'DEPLOYED'
    for check in checks:
        assert check['status'] == 'PASS' and check['exitCode'] == 0
        assert check['ready']['loaded_build']['buildId'] == build['releaseId']
    assert owner['loadedBuild']['buildId'] == build['releaseId']
    assert owner['backendSourceCommit'] == backend['sourceCommit']
    assert owner['backendBuildId'] == backend['buildId']
    assert owner['backendArtifactsVerified'] == len(backend['artifacts']) == audit['studioValidation']['build']['artifacts']
    assert owner['instanceId'] == after['discovery']['instanceId']
    assert after['originalOwner']['state']['loaded_build']['buildId'] == build['releaseId']
    preserved = after['preservation']
    assert not preserved['changedOrMissingOriginalHistories']
    assert preserved['sharedMetadataUnchanged'] and preserved['sameAgentDir'] and preserved['sameHistoryRoot']
    assert not preserved['newHistoryFiles']
    assert before['discovery']['serverId'] == after['discovery']['serverId'] == owner['serverId']
    assert before['discovery']['instanceId'] != after['discovery']['instanceId']
    old_runtime = read(O / 'backups/runtime.before.json')
    new_runtime = read(Path(after['runtimeConfig']))
    runtime_changes = sorted(k for k in old_runtime.keys() | new_runtime.keys()
                             if old_runtime.get(k) != new_runtime.get(k))
    assert runtime_changes == ['installRoot']
    old_cli = read(Path(cli['previous_config']))
    new_cli = read(Path(cli['config']))
    cli_changes = sorted(k for k in old_cli.keys() | new_cli.keys() if old_cli.get(k) != new_cli.get(k))
    assert cli_changes == ['installRoot']
    assert sha(Path(before['runtimeConfig'])) == before['runtimeConfigSha256']
    for row in cli['entries']:
        assert sha(Path(row['path'])) == row['after_sha256']
    result = {
        'schema': 'BioMindProductionDeploymentV1', 'status': 'PASS',
        'atUTC': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'core': {'sourceCommit': core['sourceCommit'], 'buildId': build['releaseId'], 'root': build['root']},
        'studioBackend': {k: backend[k] for k in ('sourceCommit', 'buildId', 'root', 'dependencyBoundary')},
        'windowsClient': {'version': '1.2.0-rc.20261009.8',
            'sourceCommit': '617114d8abd4500c546b402115a43a6b78a1c5c5',
            'binaryChangedThisRollout': False, 'freshGUIAcceptance': False,
            'reason': 'This rollout did not modify Windows client binaries; rc8 is the previously accepted version. The server/backend and original Main were updated.'},
        'acceptance': {
            'cli': [{'launcher': c['launcher'], 'status': c['status'], 'exitCode': c['exitCode'],
                     'buildId': c['ready']['loaded_build']['buildId'], 'requiredTools': c['requiredTools']}
                    for c in checks],
            'studio': {k: owner[k] for k in ('serverId', 'instanceId', 'port', 'sessionId', 'ownerGeneration',
                       'backendBuildId', 'requiredTools', 'originalOwnerAlreadyReconnectedBeforeProbe', 'resumedByProbe')},
            'coreRegressionChecks': audit['coreValidation']['totalTests'],
            'studioRegressionChecks': audit['studioValidation']['totalTests'],
            'testEvidence': '53 core checks reused with source/artifact identity verification; PC1 4 checks and Studio 67 checks ran for this change.',
            'backendArtifactFilesVerified': owner['backendArtifactsVerified'],
            'coreArtifactFilesVerified': tested['artifacts'],
            'modelRequests': 0},
        'preservation': {**preserved, 'serverIdUnchanged': True, 'runtimeChangedKeys': runtime_changes,
            'cliRuntimeChangedKeys': cli_changes, 'oldRuntimeUnchanged': True,
            'nativeIdleShutdown': studio['native_idle_shutdown'], 'tasksCancelled': 0, 'tasksReplayed': 0},
        'limitations': [
            'The historical initialization exception was suppressed; the matching reproduced defect is fixed without claiming unique retrospective causality.',
            'Prior real-model natural behavior N1/N2 remain PARTIAL; N3/N4 retain their prior PASS. This rollout made no model requests.',
            'The separate Windows latency audit/v9 draft was not released. No new Windows GUI or end-to-end latency acceptance is claimed.'
        ],
        'git': {'remote': 'https://github.com/jebwvgsid/biomind_agent_47.git',
            'coreBranch': 'release/submission-20261020',
            'studioBackendBranch': 'studio/startup-production-20261021',
            'windowsClientBranch': 'studio/windows-client-20261021',
            'finalPushReceipt': str(O / 'GIT_RESULT.json')},
    }
    evidence_names = ['build-result.json', 'backend-result.json', 'cli-deployment.json', 'studio-deployment.json',
                      'studio-owner-verification.json', 'studio-rebind.json', 'evidence/production-before.json',
                      'evidence/production-after.json', 'evidence/tested-artifacts.json', 'evidence/audit-summary.json',
                      'evidence/official-cli/receipt.json', 'evidence/project-cli/receipt.json']
    result['evidence'] = [{'path': str(O / n), 'sha256': sha(O / n)} for n in evidence_names]
    D.mkdir(parents=True, exist_ok=False)
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    (O / 'DEPLOYMENT.json').write_text(text)
    (D / 'DEPLOYMENT.json').write_text(text)
    shutil.copy2(O / 'evidence/audit-summary.json', D / 'AUDIT.json')
    shutil.copy2(O / 'evidence/pc1-tests.json', D / 'PC1_VALIDATION.json')
    shutil.copy2(S / 'docs/implementation/startup-admission-20261021/VALIDATION.json', D / 'STUDIO_VALIDATION.json')
    scripts = D / 'scripts'
    scripts.mkdir()
    for name in ('build_core.py', 'freeze_backend.py', 'deploy_cli.py', 'deploy_studio.py',
                 'rebind-managed-services.py', 'verify_cli.py', 'verify_studio_existing.py',
                 'snapshot_production.py', 'finalize_receipts.py'):
        shutil.copy2(O / 'scripts' / name, scripts / name)
    report = f'''# BioMind production update — 2026-10-21

Both supported production CLI launchers and the Studio backend now load core `{build['releaseId']}` (source `{core['sourceCommit']}`). Studio uses backend commit `{backend['sourceCommit']}`, Next build `{backend['buildId']}`. Actual JSON-host startup succeeded at both CLI entries, and the original Studio Main resumed with the same history file and the new loaded build.

The update includes the bounded shared startup admission wait and typed initialization errors, PC1 invalid-value handling (NA instead of false A/B labels), and Studio propagation of early initialization errors without later frames reopening a failed owner. AUDIT.json records the examined branches and explains which earlier work is already integrated or remains an independent draft.

Validation: {audit['coreValidation']['totalTests']} core regression checks and {audit['studioValidation']['totalTests']} Studio checks passed. The 53 previously run core checks are tied to unchanged source/compiled artifacts; the 4 PC1 checks and 67 Studio checks ran for these changes. Studio lint/typecheck/build passed. All {tested['artifacts']} core compiled artifacts and {owner['backendArtifactsVerified']} frozen Studio build files were verified. Actual deployment acceptance used no model requests.

The native service idle-stop and locked rebind preserved server identity, shared settings and all {preserved['originalHistories']} original session history files byte-for-byte. No task was cancelled or replayed, and no persistent test session was created. Runtime configuration changed only installRoot; the old release and private rollback backups remain available.

This rollout did not modify Windows client binaries; rc8 is the previously accepted version. Client and backend source use separate studio branches of the same repository, with final remote tips verified separately in GIT_RESULT.json. This rollout does not claim a fresh Windows GUI or latency acceptance. The independent latency audit/v9 draft remains separate. Prior natural behavior N1/N2 remain PARTIAL; N3/N4 retain their earlier PASS.

DEPLOYMENT.json contains runtime identities, acceptance results, preservation assertions and evidence hashes. Scripts are the exact reviewed deployment/verification operations, with local paths for this host. Private configuration, credentials and session contents are not included. Final branch tips are verified in `{O / 'GIT_RESULT.json'}` after this deployment record is committed and pushed.
'''
    (D / 'README.md').write_text(report)
    (O / 'FINAL_REPORT.txt').write_text(report)
    print(json.dumps({'status': 'PASS', 'documentation': str(D), 'buildId': build['releaseId'],
                      'originalHistoriesPreserved': preserved['originalHistories']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
