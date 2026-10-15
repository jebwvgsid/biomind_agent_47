"""Reconstruct the pinned patch and verify preserved workspace/production inputs."""
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

root = Path(__file__).resolve().parents[1]
evidence = root / 'docs/implementation/pi-output-limits_20261015'
vendor = root / 'integrations/pi_runtime/vendor/pi-output-limits'
hash_file = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
lock = json.loads((vendor / 'upstream-lock.json').read_text())
assert hash_file(vendor / 'upstream.tar.gz') == lock['archiveSha256']
assert hash_file(vendor / 'biomind.patch') == lock['patchSha256']
with tempfile.TemporaryDirectory(prefix='pi-output-limits-reproduce-') as directory:
    target = Path(directory)
    with tarfile.open(vendor / 'upstream.tar.gz') as archive:
        archive.extractall(target, filter='data')
    for name, digest in lock['baselineFiles'].items():
        assert hash_file(target / name) == digest, name
    subprocess.run(['git', 'apply', '--check', str(vendor / 'biomind.patch')], cwd=target, check=True)
    subprocess.run(['git', 'apply', str(vendor / 'biomind.patch')], cwd=target, check=True)
    for name, digest in lock['patchedFiles'].items():
        assert hash_file(target / name) == digest == hash_file(vendor / name), name
before = json.loads((evidence / 'before.json').read_text())
for name, digest in before['preexisting'].items():
    assert hash_file(root / name) == digest, name
production = json.loads((evidence / 'entrypoint-location.json').read_text())
for name, digest in production['files'].items():
    assert hash_file(Path(name)) == digest, name
original = subprocess.check_output(['git', 'show', before['head'] + ':integrations/pi_runtime/extensions/biomind.ts'], cwd=root, text=True)
current = (root / 'integrations/pi_runtime/extensions/biomind.ts').read_text()
rule = '- 大文件或长工具输出按需检索、分段读取或在计算端统计筛选，避免无关内容进入上下文；完整输出留在文件或现有作业记录，保留真实退出状态。截断后按返回路径、范围或游标续读，不为查看输出重跑分析，不用预览片段代替全量统计或必要核验。'
assert current.count(rule) == 1 and rule not in original
import re
prompts = lambda text: dict(re.findall(r'(?:export )?const (\w*RULES) = `([\s\S]*?)`;', text))
assert prompts(original) and prompts(original) == prompts(current.replace(rule + '\n', ''))
result = {'status': 'PASS', 'upstreamCommit': lock['commit'], 'patchReproduced': True,
          'preexistingFilesUnchanged': len(before['preexisting']), 'productionFilesUnchanged': len(production['files']),
          'onlySpecifiedMainPromptRuleAdded': True, 'otherRuleStringsUnchanged': sorted(prompts(original))}
(evidence / 'provenance-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False))
