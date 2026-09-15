# BioMind 后端事故：session_1789471955 阶段 A 审计

## 这次到底发生了什么

**任务只完成了输入可读性和 contig 名称兼容性两步。prepare_database 执行了四次却没有 Commit，真正的数据库构建、变异注释、ANN 汇总与最终交付均未开始。后段耗时主要用于 Recovery 上下文压缩；三个压缩事务没有一个完成有效应用，最终由用户 Ctrl+C 取消。取消后 `/exit\n` 又被当作普通输入送进了一次路由请求。**

这不是对照会话的“prepare 两候选均未执行”事故重演，也没有证据表明系统在后段持续运行 snpEff、等待超长生信进程或因缓存计数重复相加而产生约 300 万 Token。

已证实的故障链如下，各环节不能相互替代：

1. **首轮动作确有目录契约不符。** 进程 rc 0，但工作区后置条件失败。Recovery 修正目录后，后三次本地核验与确定性 gate 均通过。
2. **语义审查收到的证据不完整且部分失真。** 动作里有 typed binding values，CompletionReport 却投影为 `value_present=false`；已 Commit 的上游 contig 核验也未投影到三个审查请求。Supervisor 因此继续提出 inspection。第二段 Recovery 甚至明言重复动作不能修证据问题，又要求重跑一次。
3. **最后一次又出现可反证的模型路径判断。** 设批准工作区为 W，契约就是 `W/data`；最后 config 也写 `W/data`。Supervisor 却称它多一层。早期 config 与新复制位置不一致的问题是真实的，不能因此把最后正确的契约代入也判错。
4. **Recovery 输入随着嵌套因果证据增长，触发昂贵但未被接受的压缩。** 43 次压缩物理请求属于三个含 map 分片/最终 plan 的事务，并非 43 个完整压缩事务。前两个最终 plan 均未通过校验，回退未压缩请求；第三个事务在 map 阶段取消。后继 Recovery 仍使用 epoch 0。
5. **原任务取消有效，退出控制另有缺陷。** Ctrl+C 的本地清理、任务取消和 lease 释放有回执；带尾随换行的 `/exit\n` 被换行条件挡在控制命令分支外，产生额外路由。没有原请求迟到科学执行或结果覆盖的证据，最终操作系统退出码仍缺失。

本轮状态：**阶段 A 已完成用户消息明确范围；生产修复未实施。** 用户点名的“近300万Token与Recovery循环”任务书未找到，实际附件是对照事故的 Generate 实施书。没有把该附件的实施命令作为本轮授权，详见 [范围与复用](scope_and_reuse.md)。

## 1. 实际科学进度与逐执行回执

| 步骤 | 物理执行次数 | 本次可证明结果 |
|---|---:|---|
| check_inputs_readable | 1 | 验证并 Commit |
| check_contig_compatibility | 1 | 独立 parser 核验并 Commit；只证明名称兼容 |
| prepare_database | 4 | rc 均为 0；首轮后置条件失败，后三轮确定性通过但语义审查未通过；没有 Commit |
| build_database / annotate_variants / summarize_ann / finalize_outputs | 0 | pending；没有本次结果或交付回执 |

六次执行回执耗时合计 **8.072 秒**，不能与会话时长混为一谈。`annotated.vcf`、`ann_summary.tsv`、`report.md`、`reproduce.sh`、`stage_status.tsv` 在 provenance 中均仅为 `@expected`，无本次 producer/validation。共享目录旧文件不计入本次交付。

完整证据：[科学进度与逐轮因果说明](science_recovery.md)、[六次执行账本](evidence_science.tsv)、[结构化回执与 JSON 定位](evidence_science.json)。未重新读取正式 VCF/FASTA/GTF，也未实际运行 snpEff；本报告不证明 assembly 相同或数据库可加载。

## 2. 逐请求账本与 Token / 缓存

交付 [主会话逐请求账本 request_ledger.tsv](request_ledger.tsv)：**79 条 trace 请求记录**。其中 72 条有唯一 provider attempt 记录（71 完成、1 取消），另 6 条完成调用有 profiler/response usage，最后 1 条是取消后失败的路由。内部不可观察的 SDK/网络尝试不补造为确定物理请求。

| 请求类别 | 完成且有 usage | 输入 Token | 输出 Token | 总 Token |
|---|---:|---:|---:|---:|
| 路由 | 1 | 933 | 101 | 1,034 |
| Consultant | 5 | 91,128 | 5,391 | 96,519 |
| Planner | 3 | 94,980 | 11,505 | 106,485 |
| Generate | 6 | 160,750 | 6,827 | 167,577 |
| Supervisor | 5 | 110,345 | 4,974 | 115,319 |
| Recovery 业务回合 | 15 | 1,678,449 | 5,512 | 1,683,961 |
| Context compaction | 42 | 822,567 | 77,576 | 900,143 |
| **合计** | **77** | **2,959,152** | **111,886** | **3,071,038** |

总量与本次 run_metrics 对得上；Recovery 与压缩共 **2,584,104 Token，约 84.14%**。run_metrics 将压缩也归到 recovery，因此其 recovery 57 次不是 57 次业务恢复决策。

- 输入 cache hit **1,310,976**，miss **1,648,176**，输入命中率 **44.3024%**。hit 已包含于输入，不能再加到总 Token。
- 逐行 cache 状态为 `partially_reported_derived`：miss 是 `prompt − hit` 的本地派生；汇总的 `reported` 比逐行来源更粗。不能将两个数都称为供应商原始返回，更不能当作账单金额。
- Recovery 业务输入命中约 **68.86%**，压缩输入命中仅 **0.7625%**。这些是本次观测，不证明缓存系统失灵，也不能证明应用压缩能减少多少实际费用。
- `compaction-0078` 取消、`task_router-0079` 失败的 usage **未知，不能填 0**。3,071,038 是已回传可核对总量，不是含未知请求的最终供应商账单。
- request.json 是应用保存的 provider-visible messages，不是含全部 HTTP 包装/重试的原始网络抓包。各阶段 SDK 策略不同：已捕获 Consultant 为 1，Planner/Generate/Recovery/compactor 为 0；这不是内部重试实际次数，不据此宣称内部重试为零。

去重、映射、来源和限制见 [usage_summary.json](usage_summary.json) 与 [usage/压缩专题](usage_compaction.md)。对照会话单列 [control_request_ledger.tsv](control_request_ledger.tsv)：其 profiler 漏记一次 terminal_summary 的 4,276 Token，与主会话的闭合账本分开处理。

## 3. 压缩究竟是否应用

**没有成功应用的证据；前两个事务有明确失败与回退证据，第三个被取消。** `recovery_compaction_completed` 只说明某次 provider 请求返回，不代表摘要 plan 校验、归档激活、CAS 和最终业务请求替换全部成功。

| 压缩事务 | 物理请求构成 | 已知总 Token | 最终结果 |
|---|---|---:|---|
| cmp-a6155857… | 11 次 map + 2 次最终 plan | 314,541 | normal 模式摘要 selected material 被拒；修正又引入未知字段；safe_uncompacted_request |
| cmp-ef1ba8fd… | 14 次 map + 2 次最终 plan | 351,407 | 两次 plan 均缺 material_retrieval_state 的 source_unit_id；safe_uncompacted_request |
| cmp-c13b88d1… | 13 次 map 完成 + 1 次 map 取消 | 234,195（不含未知取消用量） | 未形成可应用的最终 plan，用户取消 |

四份最终 plan 的直接错误分别是 `COMPACTION_SELECTED_SUMMARY_REQUIRES_CAPACITY_PRESSURE`、`CONTEXT_SCHEMA_UNKNOWN_FIELD`、两次 `CONTEXT_FIELD_REQUIRED`。这些是原始响应及归档拒收记录，不是任务书假设。

后继请求 `recovery_turn-0047`、`recovery_turn-0064` 都保持 `context-epoch:0`，provider 输入分别 **178,311 → 185,407 Token**；没有新 epoch 归档/active pointer 或摘要替换。最后原任务模型请求 `schema2_context_compaction-0078` 仍是第三事务 map 调用，最终 response 为取消。全日志最后请求 `task_router-0079` 属于之后的新退出交互，不能与它混同。

Recovery 四个 episode 的首个业务请求输入为 **61,868 → 111,065 → 146,341 → 178,311 Token**。可见因果历史消息增长至约 300,088 字符，其中 `parent_evidence` 最大约 288,631 字符。嵌套保留的历史证据使每次进入 Recovery 的基础负担增加；压缩失败后又发送全量，并在后续工具回合触发新事务。正确拒收不合法摘要保护了证据，但未阻止这条高成本反复尝试路径。

完整原始请求、最终模型响应、归档对象、字段/消息组成及哈希：[compaction_evidence.json](compaction_evidence.json)。其中 `source_files` 与 `phase_archive_objects_read` 可定位文件；离线提取脚本直接重现字段拒收及 selected-material 谓词，未调用模型。

## 4. 主要等待与取消退出

所有时间为 **2026-09-15 UTC+08:00**；以下为互相可能包含的区间，不可直接相加。

| 时间区间 | 时长 | 活动 |
|---|---:|---|
| 19:42:58.294–19:44:45.463 | 107.169 秒 | 等用户代码审批 |
| 19:45:17.726–19:48:39.699 | 201.973 秒 | 第一组串行压缩 |
| 19:48:57.132–19:52:58.361 | 241.229 秒 | 第二组串行压缩 |
| 19:53:09.691–19:56:55.822 | 226.131 秒 | 第三组串行压缩，末次取消 |
| 19:56:03.492–19:56:17.481 | 13.989 秒 | 调用之间本地无遥测，具体原因未知 |
| 19:57:06.030–19:57:10.758 | 4.728 秒 | `/exit\n` 误入新的路由请求，最终 CancelledError |

最后 Recovery 节点约 **699.768 秒**；三组压缩包围区间合 **669.334 秒，占 95.7%**，其中包含 provider 与本地准备间隙。全会话 15 个 Recovery 工具合 **1.071 秒**；72 个有 attempt 遥测请求中最长约 **25.011 秒**，有持续 callback 证据。没有依据把后段等待描述为一次 600 秒 provider 超时。

Ctrl+C 在 `19:56:55.655690` 请求取消，provider adapter 在 `19:56:55.657145` 确认本地清理；`19:56:56.533231` 任务取消持久化。其后回交互提示符。`runtime_state` 和 run_metrics 都为 cancelled，兼容顶层 `execution_status` 仍为 in_progress，后者不能证明任务还在执行。

原始第二次用户输入精确为 `/exit\n`。`bio_agents.py:54363` 的 `"\n" not in user_input` 条件使退出指令未匹配；triage 又拼接全部 user turns，所以 request 0079 包含原任务和退出文本。已用源码 AST 的实际布尔条件离线复现。清理回执只证明本地取消；远端请求是否停止计费、最终 OS 退出码及日志终点之后行为仍未知。

完整 [等待/取消说明](wait_cancel.md)、[126 行区间账本](wait_intervals.tsv)、[原始事件与退出见证](evidence_wait.json)。

## 5. 根因与最小修复切片（待确认，均未实施）

| 切片 | 已证明的问题及修改边界 | 最小离线验收 |
|---|---|---|
| A1 审查事实投影 | 将 immutable action 的 candidate binding value/type/source、workspace attestation、已声明依赖的 Commit/parser refs 准确投影；candidate 与 accepted 分开，缺记录不表示值不存在 | 本次三个 CompletionReport 重放：不再与 gate/上游 Commit 矛盾；未提交绑定仍不得冒充 accepted |
| A2 布局与补证据恢复 | 明确 W、contract data_root、config 与实际位置；inspection 缺可见性优先补既有证据，只有可改变真实缺陷的动作才重试；保持有限执行预算 | 首轮真实不符仍拒；最终 W/data 不因名字 data/data 被误判；重复同动作不能获得新的“修好投影”证明 |
| A3 Recovery 历史体积 | 保留因果身份、当前失败和必要差量；父证据用可验证 refs 定点取回，避免递归携带整包旧 evidence | 四个 episode 历史重放仍可追溯全部旧 attempt，不重复注入父证据全文、不丢审批或失败身份 |
| A4 压缩有效应用与总成本边界 | 对齐实际 compactor schema 示例/反馈；保留 selected-material、coverage、CAS 校验；约束整个事务（含 map/重试）的成本和相同失败的重新触发 | 四份拒收反例仍不能非法应用；合法 plan 真正激活并出现在下一请求；同一失败不能静默多次全量 map；取消不产生新 epoch |
| A5 控制命令与取消投影 | 在模型路由前只规范化完整独立控制命令的首尾空白；保持真正多行正文语义；统一 canonical cancelled 读视图并记录退出回执 | `/exit`、尾随 LF/CRLF 均零模型调用；正文中的示例不误退出；取消后无执行；旧进度保留 |

不建议重做已经验收的 Generate 载体/缺键纠错修复、增加候选预算、关闭安全核验、把所有 Supervisor 意见当确定性事实或默认宣布步骤成功。模型真实失误、证据投影缺陷、控制路径与压缩成本问题分别处理。

## 6. 基线、离线验证与交付边界

- 基线 `d9902812603fe02aab12b00a418386efb9f935ed`，分支未切换。已有 Generate 修复 `5bc79f2` 与 9 月 6 日压缩 archive/CAS 底座复用，旧测试数量不算本轮新验证。
- [runtime_provenance.json](runtime_provenance.json) 比较三份历史 runtime.identity：每份捕获的 28 个源文件与当前字节一致；loaded_revision 仍 UNKNOWN，未捕获的关键模块不能宣称历史字节完全确定。
- 三个专题离线脚本已执行：科学历史一致性、usage/压缩拒收、等待与 `/exit` AST 见证。主 [verify_audit.py](verify_audit.py) 检查 Token 算术、请求/执行计数、取消状态和原始证据/源码保护，结果见 [verification.json](verification.json)。这些是历史见证，不是生产修复 PASS，也不是新的科学实验。
- [source_manifest.json](source_manifest.json) 给出原始证据文件大小、mtime、SHA256；[protection_before.json](protection_before.json) 记录既有工作树/index 及 918 个允许读取的已跟踪 Python 文件哈希。
- 未修改生产代码、未恢复原任务、未真实调用模型/API、未执行生信分析、未读取含凭据启动脚本或被禁止的密钥文件、未安装依赖、未自动 push。安全提交仅纳入本目录新增审计产物。

**阶段 A 到此停止，等待用户选择具体修复切片。** 缺失任务书、未回传 usage、原始 HTTP/SDK 内部重试、未捕获历史模块、远端取消和最终 OS 退出码均保留为缺口，不通过真实重跑补造。
