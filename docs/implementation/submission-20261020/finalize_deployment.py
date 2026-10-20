from pathlib import Path
import json,shutil,hashlib,datetime,ast
B=Path('/public/home/liusiqi/03.毕设');O=B/'BioMind_Production_20261020';W=B/'BioMind-worktrees/submission-production-20261020';D=W/'docs/implementation/submission-20261020';R=B/'BioMind_Original_Cases_Revision_20261020'
def read(p):return json.loads(p.read_text())
build=read(O/'build-result.json');cli=read(O/'cli-deployment.json');studio=read(O/'studio-deployment.json');connected=read(O/'evidence/studio-connected-core.json');smoke=read(O/'evidence/production-cli-smoke/receipt.json');harness=read(O/'evidence/original-case-harness-load-only.json');inputs=read(R/'evidence/final-audit.json')
assert build['result']=='PASS' and cli['status']==studio['status']=='DEPLOYED' and connected['result']==smoke['status']==inputs['status']=='PASS';assert len(harness)==4 and all(x['status']=='PASS_LOAD_ONLY' for x in harness)
for x in cli['entries']:assert hashlib.sha256(Path(x['path']).read_bytes()).hexdigest()==x['after_sha256']
record={'status':'CORE_AND_FOUR_CASE_ENTRIES_DEPLOYED','at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'branch':'release/submission-20261020','build_id':build['releaseId'],'build_source_commit':build['sourceCommit'],'development_candidate_commit':'65f3fb6a48a188ca182760114743e25f41779b9d','release_root':build['root'],'cli_entries':[x['path'] for x in cli['entries']],'studio':{'server_id':connected['serverId'],'instance_id':connected['instanceId'],'status':'ready','connected_core_checks':connected['checks'],'native_idle_shutdown':True,'windows_interaction':'NOT_RUN'},'original_case_manifest':str(R/'manifest.json'),'benchmark_entry':str(O/'benchmark-harness/biomind-entry.py'),'benchmark_manifest':str(O/'benchmark-harness/manifest.json'),'four_case_isolated_web_plugin':'pi-web-access@0.38.0, all four actual project entries passed load-only checks','shared_agent_web_plugin':{'status':'NOT_APPLIED','reason':'Existing legacy adoption receipt is no longer trusted after earlier launcher changes. The guarded maintenance audit also includes an idle cotton session, unreadable system helpers and unrelated user Slurm jobs. No locks or receipts were bypassed.'},'validation':{'source_files':build['sourceFiles'],'artifacts':build['artifactFiles'],'dependency_entries':build['dependencyEntries'],'release_tests_passed':7,'input_checks_passed':inputs['n_checks'],'git_input_blob_hashes':141,'external_raw_assets_verified':7,'cli_load':'PASS','four_case_loads':4,'studio_connected_checks':len(connected['checks']),'historical_output_files_preserved':inputs['historical_outputs_preserved'],'formal_science_reexecution':False,'formal_scoring':False},'old_cotton_session':'Idle after result; preserved, not terminated','slurm_jobs':'Preserved. User clarified the observed jobs are not the current BioMind jobs.','git_remote_push':{'status':'NOT_PUSHED','https':'No GitHub HTTPS credential available','ssh':'No existing trusted GitHub ED25519 host key; strict host checking retained'},'rollback':{'cli_command':'/public/home/liusiqi/anaconda3/envs/biomind/bin/python '+str(O/'scripts/deploy_cli.py')+' --rollback','studio_receipt':str(O/'studio-rebind.json'),'previous_release':cli['previous_release']}}
(O/'DEPLOYMENT.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
report=f'''生产部署与 Git 交付（2026-10-20）

已完成：两个正式 CLI 入口与现有 Studio 服务均切换到 {build['releaseId'][:16]}；四道原题的新输入和可配置训练入口保持生效，另建立绑定同一核心的独立联网测试入口。原始资料、旧数据目录、历史题面备份、运行结果与评分均保留。

发布分支：release/submission-20261020
工作树：{W}
冻结构建的源码提交：{build['sourceCommit']}
核心目录：{build['root']}
后续仅补充回执的 Git 提交不会改变该构建身份。

CLI：/public/home/liusiqi/.local/bin/biomind 与 {B}/BioMind/bin/biomind
Studio：{connected['serverId']}，实例 {connected['instanceId']}；已通过原生 idle-stop 正常停止空闲服务并重新启动，无用户分析任务被终止。实际连接的构建、提交、入口、Pi版本与agentDir九项检查全部通过。未做 Windows 客户端实机操作或真实模型请求。
四题入口：{O}/benchmark-harness/biomind-entry.py
调用方式：上述入口 --project-dir <原题目录> --pi-mode json
四题清单：{O}/benchmark-harness/manifest.json
此入口按原题目录绑定本次独立资源、原题题面和 input_manifest 的 SHA256。没有复制认证文件；仅在子进程环境中使用既有、已确认官方 DeepSeek 端点的密钥（显式环境变量优先），不写入日志或 Git。

验证：784个源码文件、256个构建产物和25916个依赖条目通过完整校验；7项相关Main/执行测试通过；191项四题输入及原生验证复核通过；141份Git快照文件逐字节一致，7份外部原始资产重新核对校验和；实际CLI启动及四题入口加载通过。四题入口均加载fetch_content、web_search与get_search_content。411份历史输出的元数据保留检查通过。此次没有重跑正式整题分析或评分；此前的五折原生训练与其他科学验证详见输入修订报告。

发布组合：以实际生产f755为基线，保留生产已有服务管理与资源接管能力；仅纳入本轮24项程序/技能差异及1项Main测试文案修正，排除候选中12项其他既有工作区差异。首次整理遗漏了生产已删除的3份科学预检查源码/测试，编译发现后已按生产清单删除；依赖路径检查拒绝了外部符号链接，已物化为发布内依赖。两次失败日志保留。共享文件系统的依赖读取采用8路有限并发，校验内容与原发布验证器相同，并逐项比对生产依赖清单。

尚未应用：共享agentDir的全局pi-web-access安装和web-search.json。原因是旧接管凭据仍绑定更早启动器，现有维护机制拒绝写入；维护检查还包含空闲棉花会话、无法直接读取的系统辅助进程及用户已说明无关的Slurm作业。没有通过删锁、伪造接管凭据或停止作业绕过。四题独立入口已安装同一固定插件；此项不代表所有CLI/Studio项目都已增加联网插件。此前默认Exa搜索曾返回429，不能将工具加载通过当成所有搜索请求可用。

旧进程说明：PID889042/889043/919298来自10月19日启动的棉花Hi-C会话，状态IDLE_AFTER_RESULT，最近结果记录在10月20日11:46。它不是当前运行中的分析，已保留。Studio后台服务也不等于正在跑分析，其空闲实例已更新。未停止、取消或修改任何Slurm作业。

Git远端：origin仍为原GitHub仓库；本次新分支已本地提交。HTTPS没有可用登录凭据，SSH也没有现成的GitHub主机信任记录，因此没有推送远端，没有修改认证或关闭主机验证。认证配置好后可执行：git -C '{W}' push -u origin release/submission-20261020。

回退：CLI可运行 {O}/scripts/deploy_cli.py --rollback（使用脚本指定的Python）。它先核对当前入口，恢复两份精确备份；原f755发布树和配置保留。Studio回退需先通过原生空闲关闭，再用现有rollback-managed-service-rebind.py及 {O}/studio-rebind.json 恢复绑定，然后ensure；不要在活动实例上直接覆盖配置。

部署总回执：{O}/DEPLOYMENT.json
实际验证证据：{O}/evidence
原始输入修订与科学验证：{R}/交付与验收说明.txt（该报告记录部署前阶段，当前生产状态以本部署回执为准）。
'''
(O/'部署说明.txt').write_text(report)
for name in ['DEPLOYMENT.json','部署说明.txt','build-result.json','cli-deployment.json','studio-deployment.json']:shutil.copy2(O/name,D/name)
for name in ['exact-production-delta.json','git-input-verification.json','hydrate-verified-inputs.json','studio-connected-core.json','benchmark-plugin-identity.json','release-tests.log','seal-parallel.log','git-push-dry-run.log','git-ssh-check.log']:shutil.copy2(O/'evidence'/name,D/name)
shutil.copy2(O/'benchmark-harness/manifest.json',D/'benchmark-harness-manifest.json')
shutil.copy2(O/'benchmark-harness/biomind-entry.py',D/'benchmark-entry.snapshot.py')
for p in (O/'scripts').glob('*.py'):shutil.copy2(p,D/p.name)
# Keep all earlier evidence intact; add an explicit subsequent deployment link.
(R/'后续生产部署.txt').write_text(f'原输入修订报告与审计为部署前快照，保持原样。后续生产部署已完成CLI、Studio核心与四题独立入口。当前回执：{O}/DEPLOYMENT.json\n说明：{O}/部署说明.txt\n')
print(json.dumps({'status':record['status'],'build':build['releaseId'][:16],'report':str(O/'部署说明.txt')},ensure_ascii=False))
