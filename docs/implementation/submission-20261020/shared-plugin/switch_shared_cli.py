"""CAS both official CLI launchers to the prepared shared resource scope."""
from pathlib import Path
import argparse
import ast
import hashlib
import importlib.util
import json
import os

B = Path('/public/home/liusiqi/03.毕设')
O = B / 'BioMind_Production_20261020/shared-plugin'
ENTRIES = [Path('/public/home/liusiqi/.local/bin/biomind'), B / 'BioMind/bin/biomind']

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def atomic(path, raw, mode):
    tmp = path.with_name('.' + path.name + '.shared-web.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--apply', action='store_true')
    p.add_argument('--rollback', action='store_true')
    args = p.parse_args()
    receipt_path = O / 'cli-deployment.json'
    if args.rollback:
        receipt = json.loads(receipt_path.read_text())
        for row in receipt['entries']:
            assert sha(Path(row['path']).read_bytes()) == row['after_sha256'], 'CLI changed'
        for row in receipt['entries']:
            atomic(Path(row['path']), Path(row['backup']).read_bytes(), 0o700)
        print(json.dumps({'status': 'ROLLED_BACK'}))
        return
    assert not receipt_path.exists()
    prep = json.loads((O / 'preparation.json').read_text())
    source = ENTRIES[0].read_bytes()
    assert all(e.read_bytes() == source for e in ENTRIES)
    vals = {}
    for n in ast.parse(source).body:
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in ('CONFIG', 'RELEASE'):
            vals[n.targets[0].id] = ast.literal_eval(n.value)
    config_path = Path(vals['CONFIG'])
    config_before = config_path.read_bytes()
    config = json.loads(config_before)
    assert config['agentDir'] == prep['oldAgentDir']
    new = Path(prep['newAgentDir'])
    assert (new / 'sessions').resolve() == Path(prep['sessionRoot'])
    config['agentDir'] = str(new)
    output = config_path.parent / 'cli-shared-web-runtime.json'
    result = source.decode().replace('CONFIG=' + repr(str(config_path)), 'CONFIG=' + repr(str(output)), 1).encode()
    compile(result, 'biomind', 'exec')
    receipt = {'status': 'PREPARED', 'oldConfig': str(config_path), 'newConfig': str(output),
               'oldAgentDir': prep['oldAgentDir'], 'newAgentDir': str(new), 'entries': [],
               'sessionRoot': prep['sessionRoot'], 'release': vals['RELEASE']}
    for i, entry in enumerate(ENTRIES):
        backup = O / f'cli-{i}.before'
        if not backup.exists():
            backup.write_bytes(source)
            backup.chmod(0o700)
        assert backup.read_bytes() == source
        receipt['entries'].append({'path': str(entry), 'backup': str(backup),
                                  'before_sha256': sha(source), 'after_sha256': sha(result)})
    (O / 'cli-plan.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    if not args.apply:
        print(json.dumps({'status': 'PREPARED'}))
        return
    spec = importlib.util.spec_from_file_location('shared_cli_guard', Path(vals['RELEASE']) / 'agent_core/pi_plugin_guard.py')
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    request = {'agentDir': str(new), 'cwd': str(new.parent), 'scopes': ['user']}
    plan = guard.prepare(request)
    fds = []
    changed = []
    try:
        for key in ('gate', 'lock'):
            row = plan['scopes'][0]
            fd = os.open(row[key], os.O_RDWR | os.O_NOFOLLOW)
            fds.append(fd)
            guard.lock_fds({'files': [{'fd': fd, 'path': row[key], 'mode': 'shared'}]})
        assert not any(r['fresh'] or r['legacyUnverified'] or r['adoptionPending'] for r in guard.plan(request)['scopes'])
        assert config_path.read_bytes() == config_before
        assert not output.exists()
        assert all(e.read_bytes() == source for e in ENTRIES)
        atomic(output, (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode(), 0o600)
        for entry in ENTRIES:
            assert entry.read_bytes() == source
            atomic(entry, result, 0o700)
            changed.append(entry)
    except BaseException:
        for entry in changed:
            if entry.read_bytes() == result:
                atomic(entry, source, 0o700)
        raise
    finally:
        for fd in reversed(fds):
            os.close(fd)
    receipt['status'] = 'DEPLOYED'
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': receipt['status'], 'launchers': len(ENTRIES)}))

if __name__ == '__main__':
    main()
