"""Save factual deployment receipts and copy nonsecret delivery records into the production branch."""
from pathlib import Path
import ast
import datetime
import hashlib
import json
import shutil
import subprocess

B = Path('/public/home/liusiqi/03.毕设')
O = B / 'BioMind_Production_LongTask_20261021'
W = B / 'BioMind-worktrees/submission-production-20261020'
D = W / 'docs/implementation/long-task-production-20261021'
D.mkdir(parents=True, exist_ok=True)


def read(path):
    return json.loads(path.read_text()) if path.is_file() and path.stat().st_size else None


build = read(O / 'build-result.json')
assert build and build['result'] == 'PASS'
core = read(Path(build['root']) / 'RELEASE.json')
entries = []
for path in [Path('/public/home/liusiqi/.local/bin/biomind'), B / 'BioMind/bin/biomind']:
    values = {}
    raw = path.read_bytes()
    for node in ast.parse(raw).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in ['RELEASE', 'CONFIG', 'EXPECTED']:
            values[node.targets[0].id] = ast.literal_eval(node.value)
    entries.append({'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), **values,
                    'usesNewBuild': values.get('EXPECTED') == build['releaseId']})
service = read(B / 'BioMind_Studio_Workbench_20261009/deployment/private/service.json')
env = service['environment']
studio_deployed = read(O / 'studio-deployment.json')
studio_verified = read(O / 'studio-owner-verification.json')
cli_checks = {p.parent.name: read(p) for p in (O / 'evidence').glob('*/receipt.json')}
result = {'schema': 'BioMindLongTaskProductionDeploymentV1',
          'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'sourceCommit': core['sourceCommit'], 'buildId': build['releaseId'], 'releaseRoot': build['root'],
          'sourceFingerprint': core['sourceFingerprint'], 'branch': 'release/submission-20261020',
          'cli': {'entries': entries, 'bothUseNewBuild': all(e['usesNewBuild'] for e in entries),
                  'loadChecks': {k: {'status': v['status'], 'exitCode': v.get('exitCode'),
                                    'modelRequests': v.get('modelRequests')} for k, v in cli_checks.items()}},
          'studio': {'entrypoint': env['BIOMIND_ENTRYPOINT'], 'runtimeConfig': env['BIOMIND_RUNTIME_CONFIG'],
                     'configUsesNewBuild': env['BIOMIND_ENTRYPOINT'] == str(Path(build['root']) / 'bio_agents.py'),
                     'deploymentReceipt': studio_deployed, 'ownerVerificationStatus': (studio_verified or {}).get('status', 'NOT_RUN'),
                     'hostPreflight': read(O / 'evidence/studio-host-preflight.json'),
                     'hostAccessAfterUIChange': read(O / 'evidence/host-access-after-ui-change.json')},
          'candidateAcceptance': {'targetedChecksPassed': 69, 'N1': 'PARTIAL', 'N2': 'PARTIAL', 'N3': 'PASS', 'N4': 'PASS'},
          'productionSourceEquivalence': read(O / 'evidence/production-source-equivalence.json'),
          'releaseVerification': read(O / 'evidence/release-published.json'),
          'remotePushed': False, 'windowsGUI': 'NOT_RUN'}
result['status'] = ('DEPLOYED_AND_VERIFIED' if result['cli']['bothUseNewBuild']
                    and {e['path'] for e in entries} <= {v.get('launcher') for v in cli_checks.values() if v['status'] == 'PASS'}
                    and studio_verified and studio_verified['status'] == 'PASS'
                    else 'PARTIAL_DEPLOYMENT')
(O / 'DEPLOYMENT.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
for name in ['DEPLOYMENT.json', 'deployment-plan.json', 'candidate-acceptance.json', 'MAIN_PROMPT.txt',
             'build-result.json', 'cli-plan.json', 'cli-deployment.json', 'studio-deployment.json',
             'studio-rebind.json', 'studio-owner-verification.json']:
    p = O / name
    if p.is_file():
        shutil.copy2(p, D / name)
scripts = D / 'scripts'
scripts.mkdir(exist_ok=True)
for p in sorted(O.joinpath('scripts').iterdir()):
    if not p.is_file() or p.suffix not in {'.py', '.sh'}:
        continue
    shutil.copy2(p, scripts / p.name)
proof = D / 'evidence'
proof.mkdir(exist_ok=True)
for name in ['production-source-equivalence.json', 'release-published.json', 'studio-host-preflight.json', 'studio-deploy-blocked.json', 'host-access-after-ui-change.json']:
    p = O / 'evidence' / name
    if p.is_file():
        shutil.copy2(p, proof / name)
for name, receipt in cli_checks.items():
    public = {k: receipt.get(k) for k in ['status', 'launcher', 'modelRequests', 'scientificRuns', 'defaultRuntimeConfig', 'requiredTools', 'exitCode', 'error', 'error_type']}
    public['loaded_build'] = receipt.get('ready', {}).get('loaded_build')
    public['rawEvidence'] = str(O / 'evidence' / name / 'receipt.json')
    (proof / (name + '.json')).write_text(json.dumps(public, ensure_ascii=False, indent=2) + '\n')
report = f'''BioMind 长任务生产接入记录 — 2026-10-21
状态：{result['status']}
源码：{core['sourceCommit']}
构建：{build['releaseId']}
固定发布目录：{build['root']}
生产分支：release/submission-20261020（候选8个提交已快进合入；本次交付文档另行本地提交）
两个 CLI 均指向新构建：{result['cli']['bothUseNewBuild']}
CLI 实际加载检查：{json.dumps(result['cli']['loadChecks'], ensure_ascii=False)}
首次 official-cli 初始化返回 PI_INITIALIZATION_FAILED；工作区入口通过，官方入口在未改变配置的串行复验中通过。失败原始回执保留，未据此确定根因。生产加载验收不调用模型、不执行正式分析。
Studio 配置指向新构建：{result['studio']['configUsesNewBuild']}
Studio 实际 owner 验证：{result['studio']['ownerVerificationStatus']}
Studio 当前入口：{env['BIOMIND_ENTRYPOINT']}

候选自动复查、同一 Main 唤醒及 JSON 宿主事件收尾已在候选测试接通；本次生产接入状态以 DEPLOYMENT.json 为准。
69项定向检查通过；真实模型 N1、N2 仍为 PARTIAL（分别为查找范围偏离、耗时解释依据不足），N3、N4 为 PASS。部署不会把未通过项改成通过。
生产源文件已按候选清单逐项校验791项一致；发布包由原 core-release.py 完整复制和验证。没有升级 Pi 或共享插件。
完整候选验收报告：{B/'BioMind_Long_Task_Reuse_20261021/FINAL_REPORT.txt'}
最终 Main 正文：{O/'MAIN_PROMPT.txt'}

执行脚本（使用宿主终端，原有锁/空闲检查保留）：
{core['python']['path']} {O/'scripts/deploy_cli.py'} --release {build['root']} --apply
{core['python']['path']} {O/'scripts/deploy_studio.py'} --apply
{core['python']['path']} {O/'scripts/verify_studio.py'}
CLI 已部署时不要重复运行 deploy_cli.py；cli-deployment.json 是既有应用回执。

回退：CLI 用同一 deploy_cli.py --rollback，先验证当前入口校验和，再恢复精确备份。
Studio 仅在 studio-rebind.json 存在且状态 APPLIED 时可回退：在真实宿主先用原 server-service.py stop 对明确实例执行空闲关闭，再用同目录 rollback-managed-service-rebind.py --receipt {O/'studio-rebind.json'} --output-receipt <新的绝对路径> --apply，最后 ensure。旧发布目录和用户资源保留。

部署期间曾受执行环境限制：会话看不到宿主 PID，本机网络 socket 返回 EPERM，申请提升权限及用户切换权限后复检仍如此。没有绕过 backend/configure/service 锁。当前是否已解除，以 DEPLOYMENT.json 的 Studio 实际 owner 验证为准。
{'Studio尚未接入；在真实宿主终端运行 bash '+str(O/'scripts/complete_on_host.sh')+'，脚本执行现有重绑定、实际Main加载验证、清理验收会话并提交部署记录。' if not studio_verified or studio_verified.get('status') != 'PASS' else 'Studio已在真实宿主完成重绑定及实际Main加载验证。'}
Git：仅本地提交；本次未请求远端推送。Windows 桌面 GUI 未验收。
'''
(O / 'DEPLOYMENT_REPORT.txt').write_text(report)
(D / '部署说明.txt').write_text(report)
subprocess.run(['git', 'diff', '--check'], cwd=W, check=True)
print(json.dumps({'status': result['status'], 'documents': str(D),
                  'cliBothNew': result['cli']['bothUseNewBuild'],
                  'studioNew': result['studio']['configUsesNewBuild']}, ensure_ascii=False))
