#!/usr/bin/env python3
"""M05 actual JSON priority witness; reuse Product driver and offline fixture."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time

RUNTIME = Path(__file__).resolve().parents[1]
REPO = RUNTIME.parents[1]
spec = importlib.util.spec_from_file_location('memory_driver', RUNTIME / 'testing/project-memory-witness.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path, required=True)
    evidence = parser.parse_args().evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='biomind-memory-default-priority-'))
    for name in ['home', 'agent', 'skills', 'project', 'sessions', 'memory', 'wiki']:
        (root / name).mkdir()
    (root / 'project/AGENTS.md').write_text('M05_SYNTHETIC_PROJECT_ONLY\n')
    (root / 'preferences.md').write_text('M05_SYNTHETIC_PREFERENCES_ONLY\n')
    (root / 'registry.json').write_text('{"environments":{},"tools":{}}')
    (root / 'agent/settings.json').write_text(json.dumps({'defaultProjectTrust': 'always',
        'retry': {'enabled': False}, 'cacheWarming': 'off', 'enableAnalytics': False,
        'enableInstallTelemetry': False}))
    trace = evidence / 'provider.jsonl'
    fixture = root / 'provider.json'
    fixture.write_text(json.dumps([{'content': [{'type': 'text', 'text': 'M05_PRIORITY_OK'}],
        'trace_path': str(trace), 'expect_system_contains': 'M05_SYNTHETIC_PREFERENCES_ONLY'}]))
    configuration = {'python': sys.executable, 'providerFixture': str(fixture),
        'agentDir': str(root / 'agent'), 'sessionDir': str(root / 'sessions'),
        'dataRoot': str(root / 'domain'), 'memoryRoot': str(root / 'memory'),
        'wikiRoot': str(root / 'wiki'), 'preferencesPath': str(root / 'preferences.md'),
        'archivePath': str(root / 'archive.sqlite3'), 'registryPath': str(root / 'registry.json'),
        'historyEnabled': False, 'memoryMode': 'disabled'}
    config = root / 'config.json'
    config.write_text(json.dumps(configuration))
    before_config = config.read_bytes()
    (evidence / 'startup-config.json').write_text(json.dumps(configuration, indent=2) + '\n')
    launch_trace = evidence / 'domain-launch.jsonl'
    # Test-only observation at the real domain-process launch boundary. Capture
    # only synthetic history switches; delegate to the original spawn unchanged.
    observer = root / 'observe-history.mjs'
    observer.write_text("import cp from 'node:child_process';\n"
        "import fs from 'node:fs';\nimport {syncBuiltinESMExports} from 'node:module';\n"
        "const original=cp.spawn;\ncp.spawn=function(command,args,options){\n"
        "if(Array.isArray(args)&&args.some(a=>String(a).endsWith('/python_service.py')))\n"
        "fs.appendFileSync(" + json.dumps(str(launch_trace)) + ",JSON.stringify({"
        "history_source:process.env.BIOMIND_PI_HISTORY_SOURCE,"
        "history_default:process.env.BIOMIND_PI_HISTORY_DEFAULT,"
        "history_enabled:process.env.BIOMIND_PI_HISTORY_ENABLED,"
        "history_flag:args.includes('--history-enabled')})+'\\n');\n"
        "return Reflect.apply(original,this,[command,args,options]);};\nsyncBuiltinESMExports();\n")
    guard = evidence / 'guard.jsonl'
    env = {'HOME': str(root / 'home'), 'XDG_CONFIG_HOME': str(root / 'home/.config'),
        'PATH': str(RUNTIME / '.runtime/node/bin') + ':/usr/bin:/bin', 'LC_ALL': 'C.UTF-8',
        'PI_OFFLINE': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'BIOMIND_DISABLE_DOTENV': '1',
        'BIOMIND_PI_SKILLS_ROOT': str(root / 'skills'), 'BIOMIND_PI_HISTORY_ENABLED': '1',
        'NODE_OPTIONS': '--import=' + str(RUNTIME / 'testing/offline-guard.mjs') + ' --import=' + str(observer),
        'BIOMIND_PI_TEST_ROOT': str(root), 'BIOMIND_PI_GUARD_TRACE': str(guard)}
    product = driver.Product.__new__(driver.Product)
    product.name, product.evidence, product.trace = 'product-json', evidence, trace
    product.argv = [sys.executable, '-B', str(RUNTIME / 'testing/posix-pipe-relay.py'),
        sys.executable, '-B', str(REPO / 'bio_agents.py'), '--pi-mode', 'json',
        '--project-dir', str(root / 'project'), '--pi-config', str(config),
        '--pi-node', str(RUNTIME / '.runtime/node/bin/node')]
    hashes = {name: hashlib.sha256((RUNTIME / name).read_bytes()).hexdigest()
        for name in ['dist/host.js', 'dist/extensions/biomind.js']}
    product.process = subprocess.Popen(product.argv, cwd=root, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    product.selector = selectors.DefaultSelector()
    product.selector.register(product.process.stdout, selectors.EVENT_READ, 'stdout')
    product.selector.register(product.process.stderr, selectors.EVENT_READ, 'stderr')
    product.frames, product.pending, product.stderr = [], b'', b''
    product.started, product.ready_ms = time.monotonic(), None
    checks, failure, snapshot, launches = [], None, None, []
    try:
        ready = product.wait(lambda f: f.get('type') == 'ready', timeout=90)
        assert ready['cwd'] == str(root / 'project'), ready
        product.send({'type': 'settings', 'id': 'priority-snapshot'})
        snapshot = product.wait(lambda f: f.get('type') == 'settings_result' and f.get('id') == 'priority-snapshot')
        assert snapshot['status'] == 'ok' and snapshot['history_enabled'] is False, snapshot
        launches = [json.loads(line) for line in launch_trace.read_text().splitlines()]
        assert launches and all(item == {'history_source': 'config', 'history_default': '0',
            'history_enabled': '0', 'history_flag': False} for item in launches), launches
        product.prompt('priority-provider', 'Synthetic priority witness only')
        product.result('priority-provider')
        assert len(product.requests()) == 1
        assert config.read_bytes() == before_config, 'snapshot changed startup config'
        checks.append({'id': 'M05-config-over-environment', 'status': 'PASS',
            'environment_input': '1', 'config_input': False,
            'actual_snapshot_history_enabled': snapshot['history_enabled'],
            'actual_host_history_source_at_domain_spawn': 'config', 'startup_config_unchanged': True})
    except Exception as error:
        failure = repr(error)
        checks.append({'id': 'M05-config-over-environment', 'status': 'FAIL', 'error': failure})
    finally:
        try:
            product.close()
        except Exception as error:
            failure = failure or repr(error)
    events = [json.loads(line) for line in guard.read_text().splitlines()] if guard.exists() else []
    forbidden = [event for event in events if event.get('kind') == 'network_attempt' or
        (event.get('kind') == 'sensitive_file_attempt' and not event.get('allowed'))]
    if forbidden:
        failure = failure or 'Isolation guard recorded forbidden access'
    report = {'checks': checks, 'failure': failure, 'command': product.argv,
        'synthetic_root': str(root), 'snapshot_receipt': snapshot, 'domain_launches': launches,
        'artifact_sha256_before_launch': hashes, 'isolation_forbidden_attempts': forbidden,
        'loaded_extension_sha256': next((frame.get('extension_sha256') for frame in product.frames
            if frame.get('type') == 'ready'), None),
        'evidence_type': 'actual CLI JSON using existing Product driver, POSIX relay, local scripted provider',
        'snapshot_path': 'empty settings frame invokes extension context.snapshot; no settings update',
        'limits': 'Covers config false over environment 1 for one JSON launch; no PTY, external provider, or real-user validation.'}
    (evidence / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if failure is None else 1


if __name__ == '__main__':
    raise SystemExit(main())
