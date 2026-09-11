# Generate—Execute—Recovery 定点实施（2026-09-11）

**IMPLEMENTATION STATUS：COMPLETE（本轮 FIX-R/G/O/F 实施及下述离线覆盖）。**

本轮直接实施 FIX-R/G/O/F；证据仅来自 session_1789112763 的两次指定 attempt。没有恢复原任务。本文的通过指隔离夹具，不代表原 WGS 完成。

## 基线与隔离

- 分支：`refactor/consultant-controller-authority`。
- BASE HEAD：`9514d011ea8038bd318781459329e431d2657748`。
- 最后代码提交：`021fe2e2db11ba0846cf16516ed2da4dfeb5b06c`。本文档另行提交，最终 HEAD 见交付回复。
- 原进程实际加载的代码版本：**UNKNOWN**；不能用当前 HEAD 推定运行进程版本。
- 初始工作区清单、原文件备份保存在 `/tmp/biomind-ger-fix/status.txt` 和 `backup/`。已有配置、hash/prompt、测试及文档移动保持原状。`managed_stop.py` 的既有“任务状态/总结状态”改动按 hunk 排除，没有混入提交。
- 不读取/执行启动.sh、.env、凭据；测试配置由既有离线 bootstrap 注入。审计钩子禁止网络、原工作区读取及未登记子进程。所有测试写入 `/tmp/biomind-ger-fix/`。
- 前轮性能、CLI、emission 实现直接复用：沿用状态存储/CAS/fsync/批准、单一 Plan、typed 路由、统一候选预算、现有串流显示副本和序列化器。没有新缓存、平行 ledger、Controller 语义路由或 E2 跨 attempt 正式补证事务。

## 本轮原始证据与根因

完整的静态文件路径、字节数和 SHA-256 见 [original-evidence-manifest.json](original-evidence-manifest.json)。这是受限清单，不是全仓扫描；不包含科学数据或完整请求正文。

| 事实 | 定点证据 | 判断 |
|---|---|---|
| 获批步骤要求 `validation_observation`，必填 `value`，没有文件输出要求 | 当前 `plans/active_plan.json`，`step-01-input-preflight` | CONFIRMED |
| 第一次进程 rc=0，但没有正式 observation | `attempt_361103e36597fc7ef3a0`；stdout 3729 字节、parser emission_count=0；完成端缺 `validation_observation` | CONFIRMED |
| 第二次在 Bash 第一行失败，rc=2 | `attempt_9f2ebedcfdc5c10ca67a`；approved.sh 首行为 `<script language="bash">`；stderr 报该行语法错误 | CONFIRMED |
| 包装在模型响应/正文解析时已进入候选，并非审批后插入 | `generate-0013-e71e399f9756/response.json` 的外层 shell 内嵌 script；原 approved.sh hash 为 `f293751ace0189aeaffdbbfc484a5e83b18870d5e35362327d742be4f0e98778` | CONFIRMED |
| 两次真实 Generate 请求的正式模板遗漏 observation | `generate-0007-1ecd79fda1c3/request.json` 和 `generate-0013-e71e399f9756/request.json` 均有通用 observation 示例，但 `approved_effect_emission_template.declarations` 只有空 artifact/side-effect 组 | CONFIRMED |
| 重试失败的 inspection scope 被旧验证指针阻挡 | 原 `verification_attempt_id` 仍指第一次；已获准 `retry_step` envelope 对应第二次 action；旧生产 scope 例外只覆盖 diagnostic probe | CONFIRMED |
| 原终止摘要串到了第一次 attempt | 原 `managed_run_stop.scope.attempt_id` 指第一次，主异常是 `INSPECTION_REF_SCOPE_MISMATCH: attempt_id`，而第二次 exit_status 为 rc=2 | CONFIRMED |
| 报告保护是独立的真实冲突 | 三个批准目标已经有报告；旧 Stop identity `managed-stop:2b6896ff3f2e7b620de8a618663dc8effff86041cfb41f5375f336ca187ee108`，本次写入者为 `managed-stop:fbc3d4fea71c73f432830b7a146cab44663871dd625dcd328e9085441d258579` | CONFIRMED；不授权覆盖 |

第一次 approved.sh hash：`93d4351466febeeb18fe385e8c4749b416d1b431885429b6bb7f36806f44322f`；stdout hash：`d08375fb040df13d86057d860ef4db1f8b90854dfef3335ead5dd92391632bf3`。两次 `executed.sh` 是现有日志/退出状态包装，调用各自 approved 文件；它们本来就不应与 approved.sh 同 hash。本轮测试比较“审批正文 = approved 文件字节”，并核验包装调用及 parser 的原始 stdout hash。

## 实际修改

- **FIX-R**：`inspection_evidence.py` 允许精确重试 envelope 的新 attempt，且旧验证指针必须匹配该 envelope 的父 attempt；当前读取仍校验活动 packet/episode、登记、scope、digest。普通 retry 可沿已消费 receipt 读取精确父失败，历史对象标为 `historical_diagnostic_only` / `formal_evidence_reuse=false`。`recovery_projection.py` 将发布不可用保留为现有 FailurePacket 的诊断；已发布事件在 attempt 推进后的迟到回放只记诊断，不改当前 head。候选错误无 execution attempt/rc。
- **FIX-G**：正式块解析保留 heredoc/字符串数据，拒绝正文开头的嵌套协议包装；普通 Generate、Recovery Generate 和 Execute 重载都走正文检查。Bash 用与 Execute 相同方式解析的 Bash、显式 argv、最小环境及 `--noprofile --norc -n`；Python 用 AST；检查器超时/不可用单列。格式/语法错误进入现有持久修正预算。审批展示内容直接冻结；启动前再核验 approved 字节及 hash。managed 用户编辑先撤销旧候选，经 Generate 再批准，不能直接沿用旧授权运行。
- **FIX-O**：在现有 `_declared_emission_rows` / 序列化器内加入批准的 observation 声明。真实检查的值可以是负面或显式 UNKNOWN；字段缺失仍不满足合同。不改 parser，不从 rc=0/echo 造 PASS，不要求额外文件。完成端反馈精确缺失身份及已提供的正式模板。
- **FIX-F**：摘要分别投影当前失败和精确授权父 attempt 的 rc、排放数、观察数；候选有效性、进程成功、正式证据满足、步骤完成分开，不知道的值为 UNKNOWN。runtime/report 错误保留为次要故障。现有同源同内容报告可幂等复用；异源/不同内容和原子发布竞争继续受保护，交付未完成，不递归恢复。显示中的“成功”改为“进程退出成功，待正式验证/提交”，原 stdout/stderr 不变。

每个代码提交及实际文件清单见 [commits.json](commits.json)，没有 push。

## 两条组合验收

**7.1 通过**：合成持久状态记录 attempt-1 rc=0/缺观察和 attempt-2 rc=2/包装语法错误。走生产 binder → Recovery seed → inspection reader → RecoveryAgentLoop 的真实工具分发 → typed directive；第二次是直接故障，第一次只通过精确父授权成为历史诊断。错误旧 ref 返回可解释工具错误。重载后来源不变；终止摘要分别列出 rc=0、rc=2，报告冲突不盖掉主因。

**7.2 通过**：生产图、fake provider、真实审批入口（测试提供 y）和生产 executor 的隔离小型 Bash 子进程。第一次普通 echo rc=0 → parser 0 条排放 → 完成端拒绝 → Recovery → 内嵌 script 候选审批前拒绝（attempt 为空）→ 同一持久预算内合规模板 → 实测本地 tiny input → 原 stdout → parser → durable → Supervisor/CommitStep。每组 3 次 Generate、1 次 Recovery、2 次批准、2 次执行、1 次 CommitStep。UI 开/关均通过；开启组还经过实际 LangGraph 审批中断/恢复。现有状态机将下一步骤选为 running，但没有为它生成/执行科学命令或创建 attempt。

夹具计数与 hash：[UI 关闭](chain2-ui-False.json)、[UI 开启](chain2-ui-True.json)。这些是合成 task/attempt，不得写回原 session。

## 最终独立提交验收

最终验收：**71 passed in 49.11s，exit 0，SOURCE_UNCHANGED=True**。生产代码和全部测试源来自独立导出；测试后逐一核对 **433 个 Python 文件** 与提交 `1e2dfd0eba5bced8fb46fe187583247c21327e45` 的 Git blob 完全一致，差异为 0，不依赖已有 dirty 源码或 dirty 测试。[逐项结果](isolated-final-results.json)、[源一致性证明](committed-source-verification.json)、[原证据及保护边界](boundary-verification.json)。

最初独立导出使用旧测试夹具，68 项中有 8 项失败；其中旧 fake 协议无法进入当前 Consultant/Planner，后续又发现旧 no-output 夹具缺少本轮所需的 observation 声明。这些仅在本轮独立测试夹具中补齐，未修改既有 dirty 测试，也没有放宽生产 Plan/协议校验。最终以下命令整体通过。

```bash
TMPDIR=/tmp/biomind-ger-fix/tmp \
BIOMIND_GER_ARTIFACTS=/tmp/biomind-ger-fix/committed-acceptance \
BIOMIND_OFFLINE_RESULTS=/tmp/biomind-ger-fix/isolated-final-results.json \
/public/home/liusiqi/anaconda3/envs/biomind/bin/python \
/tmp/biomind-ger-fix/committed/tests/offline_managed_bootstrap.py \
/tmp/biomind-ger-fix/committed -q \
tests/test_ger_failure_boundary.py tests/test_ger_retry_history.py \
tests/test_ger_script_boundary.py tests/test_ger_observation_boundary.py \
tests/test_ger_combined_workflow.py tests/test_ger_retry_summary.py \
tests/test_ger_reload_controls.py tests/test_declared_effect_emission.py \
tests/test_inspection_workflow.py \
tests/test_inspection_evidence.py::test_I11_durable_attempt_advance_revokes_existing_reader_and_old_seed_registry \
tests/test_cli_display.py::test_formal_parser_receipt_is_identical_with_ui_on_off
```

## 测试命令与边界

使用 Python：`/public/home/liusiqi/anaconda3/envs/biomind/bin/python`。没有激活/切换/安装科学环境。共同命令前缀：

```bash
TMPDIR=/tmp/biomind-ger-fix/tmp \
BIOMIND_GER_ARTIFACTS=/tmp/biomind-ger-fix/acceptance \
BIOMIND_OFFLINE_RESULTS=/tmp/biomind-ger-fix/verified-results.json \
/public/home/liusiqi/anaconda3/envs/biomind/bin/python \
/tmp/biomind-ger-fix/candidate/tests/offline_managed_bootstrap.py \
/tmp/biomind-ger-fix/candidate -q \
tests/test_ger_failure_boundary.py tests/test_ger_retry_history.py \
tests/test_ger_script_boundary.py tests/test_ger_observation_boundary.py \
tests/test_ger_combined_workflow.py tests/test_ger_retry_summary.py \
tests/test_ger_reload_controls.py tests/test_declared_effect_emission.py \
tests/test_inspection_workflow.py tests/test_failure_packet_incident_lineage.py \
tests/test_inspection_evidence.py::test_I11_durable_attempt_advance_revokes_existing_reader_and_old_seed_registry \
tests/test_plan_local_preaction_convergence_v2.py::PlanLocalRealGraphTests::test_graph_c_same_malformed_representation_converges \
tests/test_plan_local_preaction_convergence_v2.py::PlanLocalRealGraphTests::test_approval_identity_failure_stops_before_attempt_allocation \
tests/test_cli_display.py::test_formal_parser_receipt_is_identical_with_ui_on_off
```

该次 **77 passed、exit 0、SOURCE_UNCHANGED=True**；逐项耗时见 [verified-results.json](verified-results.json)。之后新增 managed 编辑重新批准的断言，并使 Bash 路径解析与 Execute 一致，重跑 `test_ger_reload_controls.py test_ger_script_boundary.py`：**16 passed、exit 0**，见 [g-approval-results.json](g-approval-results.json)。这两次是开发阶段工作区导出的验收，最终独立提交证据以前述 71 项为准，不累加重复项。上述耗时是测试耗时，生产/WGS 性能收益 **UNKNOWN**。

旧大组曾跑出失败，未记为通过。以同一组夹具回放修复前生产快照（初始工作区备份及 BASE HEAD）：`test_inspection_evidence.py` 加两条旧 resume 测试，**36 failed、10 passed**。主要是旧 inspection 夹具没有 provenance attempt 登记、旧四对象/旧工具返回类型假设、旧 fake runner 不接受现有 `managed_log_paths`。具体清单见 [baseline-regression-summary.json](baseline-regression-summary.json)。本轮使用真实登记/CAS 的 `test_inspection_workflow.py`、新 scope 控制和两条组合链验收；不声称整仓测试通过。迟到已发布事件如今不再抛未捕获 scope 异常，旧“必须抛异常”断言不适用，新测试验证状态不变及明确诊断。

| 最低矩阵 | 实际覆盖 |
|---|---|
| G1/G2/G4/G5/G6/G7 | 正式协议、纠错 wrapper、多段/未闭合、中文 heredoc/字符串、语法 sentinel、超时/不可用 |
| G3/G8 | 生产 Execute 重载与审批恢复；变更生成引用被判 stale；批准文件字节变化被拒；managed 编辑重新请求候选/批准 |
| O1/O2/O5/O6 | echo 不通过；真实排放 → raw hash → durable → 验证/提交；重载 receipt 不变；仅观察步骤无需文件 |
| O3/O4 | False/UNKNOWN 如实排放；缺 value 不满足合同；错 identity/坏 JSON 被拒；独立 contig predicate 的负面控制不能提交 |
| R1/R2/R3/R4 | 当前与父历史分开；不依赖 current_attempt；错误 ref 成工具诊断；候选失败无 attempt/rc |
| R5/R6 | 真实 CAS/迟到已发布事件/活动 attempt 推进、持久重载；未授权 scope 拒绝；现有 lineage/budget 回归 |
| F1/F2/F3/F4 | 两次摘要；报告同源幂等、异源保护；原子 link 竞态故障注入；写失败为次要原因、重载不递归 |
| X1/X2/X3 | UI 开关与 raw/parser hash 对照；相同错误候选预算收敛；取消透传和真实审批中断恢复 |

## 未覆盖与恢复前事项

- 真模型/API/WGS：**NOT RUN**；原任务：**NOT RESUMED**。不加载原 session 的业务状态接口，不重建索引、不修改历史输出。
- 只测 Linux 本机 Bash 完整执行链；Python 仅做安全 AST/协议控制回归，Python/R 完整执行图 NOT RUN。R 的安全语法检查明确 unavailable，未运行 R。Bash `-n` 不能验证动态 eval/source、工具安装或科学正确性。
- 杀进程/重启 OS 的崩溃矩阵、真实多进程报告竞争、所有平台/环境排列：**NOT RUN**。已测持久重载、CAS、文件发布竞态注入、审批中断和取消；不把这些扩大为全部故障恢复覆盖。
- 旧固定报告目标冲突仍属正式交付未满足。没有额外批准新目录/新版本，也没有覆盖原文件。
- 后续恢复需另行核对宿主进程、实际失败事件、当前批准/claim/directive、已有输出及原报告冲突；不能因离线 CommitStep 通过而自动续跑。
- 原始证据清单 20 个指定文件在本轮静态核验前后 hash 相同；未修改原 approved.sh、stdout/stderr、exit_status、计划状态、目标报告。该清单不代表对全目录作了新审计。
