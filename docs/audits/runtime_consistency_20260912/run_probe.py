"""Audited runner wrapper: original capture, guards, assertions and timeout unchanged."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

export, results = map(lambda s: Path(s).resolve(), sys.argv[1:3])
if results.exists():
    raise SystemExit('Refusing to overwrite a result directory')
spec = importlib.util.spec_from_file_location('offline_runner', export/'tests/cli_model_progress_support.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
manifest = json.loads((export/'export_manifest.json').read_text())
start = time.monotonic()
error = None
try:
    code = runner.launch(export, results, sys.argv[3:])
except Exception as exc:
    code = None
    error = {'type':type(exc).__name__, 'message':str(exc)}
rows = json.loads((results/'pytest-results.json').read_text()) if (results/'pytest-results.json').exists() else []
drift = [p for p,h in manifest['files'].items() if hashlib.sha256((export/p).read_bytes()).hexdigest()!=h]
receipt = {'returncode':code,'error':error,'elapsed_seconds':time.monotonic()-start,
           'capture':'pytest default (no -s)', 'source':manifest['source'],
           'source_signature':manifest['source_signature'],'parent_source_drift':drift,
           'call_passed':sum(r.get('phase')=='call' and r.get('outcome')=='passed' for r in rows),
           'call_failed':sum(r.get('phase')=='call' and r.get('outcome')=='failed' for r in rows),
           'last_events':rows[-4:]}
(results/'receipt.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps(receipt))
