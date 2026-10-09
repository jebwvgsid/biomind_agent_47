"""Export explicit implementation source to /tmp; never import application config."""
from pathlib import Path
import json, shutil, hashlib
import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--root', required=True)
parser.add_argument('--target', required=True)
args = parser.parse_args()
ROOT = Path(args.root).resolve()
TARGET = Path(args.target).resolve()
if not str(TARGET).startswith('/tmp/'):
    raise SystemExit('Test export target must be inside /tmp')
if TARGET.exists() and any(TARGET.iterdir()):
    raise SystemExit('Export target must be empty; use a new /tmp directory for each revision')
TARGET.mkdir(parents=True, exist_ok=True)
manifest = {}
for folder in ('agent_core', 'execution', 'prompts', 'memory', 'skills', 'tests', 'tools', 'integrations', 'apps'):
    source = ROOT / folder
    if not source.exists(): continue
    for path in source.rglob('*'):
        if not path.is_file() or path.is_symlink() or '__pycache__' in path.parts: continue
        if path.suffix not in {'.py', '.json', '.yaml', '.yml', '.md', '.txt', '.tsv', '.sh', '.toml', '.gz'}: continue
        relative = path.relative_to(ROOT)
        target = TARGET / relative; target.parent.mkdir(parents=True, exist_ok=True)
        contents = path.read_bytes()
        target.write_bytes(contents)
        manifest[str(relative)] = hashlib.sha256(contents).hexdigest()
for name in ('bio_agents.py', 'supervisor.py', 'memory_manager.py', 'package_manager.py', 'text_rag.py'):
    contents = (ROOT/name).read_bytes()
    (TARGET/name).write_bytes(contents)
    manifest[name] = hashlib.sha256(contents).hexdigest()
(TARGET/'software_registry.json').write_text('{}\n')
(TARGET/'export_manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2))
print(TARGET)
print(hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest())
