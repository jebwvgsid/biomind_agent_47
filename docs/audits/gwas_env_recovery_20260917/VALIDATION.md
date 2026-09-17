# 审计验证命令、结果与边界

所有命令从 `/public/home/liusiqi/03.毕设/Biomind_v1` 执行。Python 见证不导入真实 config/startup，不读取 `.env.api_keys`；需要应用依赖的 worker 使用已有 offline bootstrap 与冻结源，模型/安装等叶子明确替换。外部真实进程仅限环境笔记列出的版本、R namespace 与 VCF 样本查询，以及原初检的隔离 Bash 重放。

| 验证 | 精确入口 | 本轮结果 |
|---|---|---|
| A/B/C 版本及冻结源 | `python docs/audits/gwas_env_recovery_20260917/witnesses/compare_versions.py` | 3,095 个身份检查，0 mismatch；905交付文件与当前相同 |
| 保护边界 | `python docs/audits/gwas_env_recovery_20260917/witnesses/verify_boundaries.py` | 1,066 个源码/测试、原session/报告/registry文件，0变化；staged无审计外文件 |
| 正式输入、原脚本与问答效果 | `python docs/audits/gwas_env_recovery_20260917/witnesses/input_planner.py` | VCF/表型215个相同且同序ID；Q215×3顺序未知；隔离原脚本rc0、四产物hash与现场相同；三个原ask候选的真实效果编译结果与拒收相符 |
| 环境/消息/真实微小进程 | `/public/home/liusiqi/anaconda3/envs/biomind/bin/python docs/audits/gwas_env_recovery_20260917/witnesses/environment_audit.py` | 退出0；12组旧/新同条件分支；4个实际Consult请求摘要；新事实丢失和既有event去重见证；真实bcftools query215；56个导入冻结模块hash匹配 |
| 有界环境索引 | `/public/home/liusiqi/anaconda3/envs/biomind/bin/python docs/audits/gwas_env_recovery_20260917/witnesses/environment_inventory.py` | 退出0；44prefix、58候选、1次bedtools版本及3次R namespace检查；无安装 |
| Installer旧新入口 | `/public/home/liusiqi/anaconda3/envs/biomind/bin/python docs/audits/gwas_env_recovery_20260917/witnesses/environment_installer_comparison.py` | 退出0；旧partial候选语句选择候选，新入口同条件不发现；成功Skill登记优先级增强保留 |
| 正式核验、typed、因果、CLI | `python docs/audits/gwas_env_recovery_20260917/witnesses/run_failure_witness.py` | 从持久源码构造全新隔离候选并已复验；当前3项/前置2项现状见证均通过，真实reconciliation与原对象相等；不是修复后通过 |
| Recovery首次/后续预算、压缩与引用 | 见下方 stage/capture/restart 命令 | 原32k门准确复现；完整假设首请求94,760估算tokens；大首轮/后续/分段/有限停止；8正文跨进程同hash和权限反例；78个实际导入生产文件与冻结源相同 |

其中 `compare_versions.py` 会额外检查仍存在的原交付候选 `/tmp/biomind-unified-cumulative-release/candidate`。即使该临时副本日后清理，持久 A快照、B清单/增量和本次 current_source仍在；应依据持久清单复核，不把临时路径缺失当作生产变化。

## Recovery 独立进程复现

以下命令每次创建新的隔离目录，并在两个独立进程中执行 capture/restart；不依赖此前 `/tmp`。stage 使用持久 current_source、保存的 bootstrap 和已保全的历史统计。

```bash
biomind_audit_sandbox=$(/usr/bin/python3 -I -S /public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/witnesses/recovery_budget_reconstruction.py stage)
env -i PATH=/usr/bin:/bin TMPDIR="$biomind_audit_sandbox/tmp" BIOMIND_DISABLE_DOTENV=1 /public/home/liusiqi/anaconda3/envs/biomind/bin/python -I /public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/witnesses/recovery_budget_reconstruction.py capture --sandbox "$biomind_audit_sandbox"
env -i PATH=/usr/bin:/bin TMPDIR="$biomind_audit_sandbox/tmp" BIOMIND_DISABLE_DOTENV=1 /public/home/liusiqi/anaconda3/envs/biomind/bin/python -I /public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/witnesses/recovery_budget_reconstruction.py restart --sandbox "$biomind_audit_sandbox"
```

本轮实际 sandbox 是 `/tmp/biomind-recovery-budget-audit-ca_01bht`。实际 worker 另使用清空的父环境、`BIOMIND_DISABLE_DOTENV=1` 及隔离临时路径，启动后由 offline bootstrap 阻止真实项目读写、网络和外部进程；详细入口见脚本 `bootstrap_worker()`。保存结果的 `recovery_evidence_pack.py --sandbox <上述实际目录>` 接收 sandbox，产生本审计目录中的 summary/request/27-ref矩阵/重启证据。该工具不作为业务 provider 或科学执行入口。

可回看 [Recovery原始结果](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/recovery_capture.json)、[重启结果](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/recovery_restart.json)、[精确请求分项](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/recovery_full_request_components.tsv)。与原现场不同的唯一容量假设只在隔离 worker 内暂时放宽旧字符门，finally恢复；原门失败另有独立见证。

## 已复用的验证与未运行项

上次统一交付证据保留在 `docs/implementation/environment_binding_unified_20260917/`。本轮905文件哈希匹配允许复用其已明确覆盖的 binding/helper/安装授权和 tiny graph 边界；没有重新宣称旧377测试覆盖当前GWAS。新增见证针对发现早退、实际消息、输入错误、完整预算、因果投影，避免重跑整仓或科学任务。

未运行：真实模型/API/SDK HTTP、安装升级、共享环境修改、原session恢复、报告writer原路径重入、完整GWAS/WGS、Q样本映射确认、关联p值/kinship/plots/候选区间。Recovery未动态覆盖多层merge或完整Node压缩CAS激活后继续业务；环境真实动作由审计显式连接，不是自动Consult/真实HITL/Commit端到端。

## 文档与提交收尾

`witnesses/validate_artifacts.py` 检查本轮JSON/TSV/Python语法、证据矩阵路径、报告链接、切片真实函数名与已记录源身份的关键字段；不调用生产或科学工具。收尾运行 `git diff --cached --check`，暂存名单必须全部以本审计目录开头。精确文件范围保存在 `AUDIT_FILES.tsv`（自身不计入其哈希表）；完整commit hash由任务完成回执给出，避免把自引用commit hash写入本次提交。

“通过”均指相应审计断言/现状缺陷被复现、证据身份一致，不能写成GWAS修复或科学任务完成。
