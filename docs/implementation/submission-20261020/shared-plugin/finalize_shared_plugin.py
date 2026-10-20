"""Record verified deployment facts; keep credentials and session bodies local."""
from pathlib import Path
import datetime
import hashlib
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET

B = Path('/public/home/liusiqi/03.毕设')
O = B / 'BioMind_Production_20261020'
S = O / 'shared-plugin'
D = B / 'BioMind-worktrees/submission-production-20261020/docs/implementation/submission-20261020'

def read(path):
    return json.loads(path.read_text())

def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def main():
    prep = read(S / 'preparation.json')
    cli = read(S / 'cli-deployment.json')
    studio = read(S / 'studio-verification.json')
    manage = read(S / 'native-management-check.json')
    run = S / 'cli-network'
    network = read(run / 'receipt.json')
    assert network['status'] == 'PASS_NCBI' and studio['status'] == 'PASS' and manage['status'] == 'ok'
    for row in cli['entries']:
        assert hashlib.sha256(Path(row['path']).read_bytes()).hexdigest() == row['after_sha256']
    responses = []
    for p in sorted(run.glob('tool-*.json')):
        data = read(p).get('structuredContent', {})
        for row in data.get('urls', []):
            assert row['status'] == 200 and row['error'] is None
            content = row['content']
            result = {'url': row['url'], 'status': row['status'], 'mimeType': row['mimeType'],
                      'bodySha256': hashlib.sha256(content.encode()).hexdigest(), 'bodyBytes': len(content.encode())}
            if 'json' in row['mimeType']:
                ids = json.loads(content)['esearchresult']['idlist']
                assert ids == ['19304878']
                result['idlist'] = ids
            else:
                ids = [n.text for n in ET.fromstring(content).findall('.//PMID')]
                assert '19304878' in ids
                result['PMIDs'] = ids
            responses.append(result)
        for row in data.get('queries', []):
            assert row.get('error') is None and row.get('results')
            urls = [x['url'] for x in row['results']]
            assert 'https://pubmed.ncbi.nlm.nih.gov/19304878/' in urls
            responses.append({'query': row['query'], 'provider': row['provider'],
                              'resultUrls': urls, 'error': None})
    assert len(responses) == 3
    old, new = Path(prep['oldAgentDir']), Path(prep['newAgentDir'])
    assert (new / 'sessions').resolve() == (old / 'sessions').resolve()
    assert hashlib.sha256((old / 'settings.json').read_bytes()).hexdigest() == prep['settings_source_sha256']
    original, current = read(old / 'settings.json'), read(new / 'settings.json')
    assert {k:v for k,v in original.items() if k != 'packages'} == {k:v for k,v in current.items() if k != 'packages'}
    acceptance = {
        'status': 'PASS', 'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'sharedAgentDir': str(new), 'legacyAgentDir': str(old), 'sessionRoot': prep['sessionRoot'],
        'nativeManagementCheck': 'OK', 'cliDefaultRuntime': True,
        'cliModelRequests': 1, 'networkCalls': responses,
        'studioChecks': studio['checks'], 'studioOwnerMainExecution': False,
        'studioInstanceId': studio['discovery']['instanceId'],
        'originalSettingsPreserved': True, 'settingsOtherThanPackagesPreserved': True,
        'authAndModelStorePrivateLocalCopy': True, 'credentialsInGit': False,
        'oldCottonProcessStopped': False, 'slurmJobsModified': False,
        'historyLayoutPreserved': True, 'guardRefusalTestsPassed': 4,
        'plugin': {'name': 'pi-web-access', 'version': '0.38.0', 'distSha256': prep['package_dist_sha256']},
        'limits': ['Studio service/package checks performed; no Windows UI or Studio owner model request.',
                   'Three successful requests demonstrate this acceptance only, not future provider quota guarantees.']
    }
    write(S / 'ACCEPTANCE.json', acceptance)
    deployment = read(O / 'DEPLOYMENT.json')
    deployment.update(status='CORE_FOUR_CASES_AND_SHARED_WEB_DEPLOYED', at_utc=acceptance['at_utc'])
    deployment['shared_agent_web_plugin'] = {
        'status': 'DEPLOYED', 'agentDir': str(new), 'package': 'pi-web-access@0.38.0',
        'method': 'Fresh native-guarded shared scope; supported CLI and Studio entries rebound.',
        'sessionRoot': prep['sessionRoot'], 'acceptance': str(S / 'ACCEPTANCE.json')}
    deployment['studio']['instance_id'] = studio['discovery']['instanceId']
    deployment['studio']['shared_plugin_checks'] = studio['checks']
    deployment['studio']['connected_core_checks'] = {
        **deployment['studio']['connected_core_checks'],
        'coreBuild': studio['context']['buildId'] == deployment['build_id'],
        'coreCommit': studio['context']['sourceCommit'] == deployment['build_source_commit'],
        'agentDir': studio['context']['agentDir'] == str(new)}
    deployment['git_remote_push'] = {'status': 'IN_PROGRESS', 'authentication': 'GitHub CLI device authorization completed as jebwvgsid'}
    deployment['rollback']['shared_cli_command'] = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python ' + str(O / 'scripts/switch_shared_cli.py') + ' --rollback'
    deployment['rollback']['shared_studio_receipt'] = str(S / 'studio-rebind.json')
    write(O / 'DEPLOYMENT.json', deployment)
    report = f'''共享联网插件与 GitHub 认证补充交付（2026-10-20）

全局插件已部署到正式 CLI 和 Studio 共用的新目录：
{new}
原目录 {old} 保留；并未删除旧锁、伪造接管回执、关闭棉花会话或取消 Slurm 作业。

之前未能更新有两个原因：旧资源接管回执仍绑定旧启动器，而正式入口已升级，导致写入校验失效；旧维护审计还保守地把账户下其他 Slurm 作业纳入占用。这次通过原生 guard 初始化全新共享目录，并用原生插件安装器安装已审核的 pi-web-access 0.38.0，再原子切换正式 CLI 与通过 native idle-stop 正常切换空闲 Studio。

原有 output-limits 插件、扩展、模型配置、compaction 等设置保留。auth.json 与 models-store.json 仅在本机同一用户的私有目录中复制（0600），未进入 Git、日志或聊天。新目录的 sessions 链接到原 sessions 根目录，保留 Pi 的按项目分目录布局；原会话正文、分析结果与评分未改动。

验收：
1. 正式 CLI 默认配置加载 fetch_content、web_search、get_search_content。
2. 一次真实模型请求执行三次小型联网调用：NCBI ESearch JSON 与 EFetch XML 均 HTTP 200，正文 ID/PMID 均为 19304878；Exa web_search 返回对应 PubMed 页面，本次未出现此前的 429。
3. Studio 当前实例 {studio['discovery']['instanceId']} 已就绪；真实原生插件接口报告同一新 agentDir、pi-web-access 0.38.0 和原核心构建。7 项共享目录检查全部通过；未做 Windows UI 操作或 Studio owner 模型请求。
4. 正式 CLI 原生插件管理 check 返回 OK，旧接管问题不再阻断新默认目录；4 项迁移拒绝路径测试通过。

GitHub：已通过本人浏览器设备授权登录 jebwvgsid，已配置 Git 凭据助手；本机 gh 位于 /public/home/liusiqi/.local/bin/gh。认证状态可用 gh auth status 查看，不要使用 gh auth token 向聊天输出令牌。认证配置文件为当前用户私有（0600），不在仓库内。分支 release/submission-20261020 的最终远端状态见 ../GIT.json。

回退：
CLI 使用 {O}/scripts/switch_shared_cli.py --rollback（Python 环境同当前部署），先核对哈希再恢复两份精确备份。
Studio 先使用现有 server-service.py stop 进行原生空闲关闭，再用原 rollback-managed-service-rebind.py --receipt {S}/studio-rebind.json --output-receipt <新的绝对路径> --apply，最后 ensure。新旧资源与会话均保留。若还需回到更早核心，先回退这次共享目录切换，再使用此前核心部署回执，避免覆盖新的入口。

核心构建 d0cf979998965759 未重编译；此次新增代码仅为部署维护脚本及证据，四题输入和运行结果保持上一轮状态。
验收摘要：{S}/ACCEPTANCE.json
本机完整证据：{S}
'''
    (S / '交付说明.txt').write_text(report)
    current_report = O / '部署说明.txt'
    if not (O / '部署说明_共享插件更新前.txt').exists():
        shutil.copy2(current_report, O / '部署说明_共享插件更新前.txt')
    current_report.write_text(report + '\n此前核心、四题输入部署详情见 部署说明_共享插件更新前.txt；其中未安装/未认证状态已被本补充取代。\n')
    target = D / 'shared-plugin'
    target.mkdir(exist_ok=True)
    for name in ['prepare_shared_agent.py', 'switch_shared_cli.py', 'rebind-shared-agent.py',
                 'test_shared_rebind.py', 'deploy_shared_studio.py', 'verify_shared_cli.py',
                 'verify_shared_studio.py', 'finalize_shared_plugin.py']:
        shutil.copy2(O / 'scripts' / name, target / name)
    for name in ['ACCEPTANCE.json', '交付说明.txt', 'preparation.json', 'cli-deployment.json',
                 'studio-rebind.json', 'studio-deployment.json', 'studio-verification.json',
                 'native-management-check.json']:
        shutil.copy2(S / name, target / name)
    shutil.copy2(O / 'DEPLOYMENT.json', D / 'DEPLOYMENT.json')
    shutil.copy2(current_report, D / '部署说明.txt')
    frozen = B / 'BioMind_Unified_Core_20261009/delivery/service/plugin-lifecycle/59f269bcd8027d40b8685feffd14a656176d5817/server/rebind-managed-services.py'
    diff = subprocess.run(['diff', '-u', str(frozen), str(O / 'scripts/rebind-shared-agent.py')], capture_output=True, text=True)
    assert diff.returncode == 1
    (target / 'rebind-shared-agent.diff').write_text(diff.stdout)
    print(json.dumps({'status': 'PASS', 'networkRequests': len(responses), 'studioChecks': len(studio['checks']), 'report': str(S / '交付说明.txt')}, ensure_ascii=False))

if __name__ == '__main__':
    main()
