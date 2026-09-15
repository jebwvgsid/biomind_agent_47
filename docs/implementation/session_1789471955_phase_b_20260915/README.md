# session_1789471955 — 阶段 B 交付

文档标识：**BM-S1789471955-PHASE-B-V1**。本轮只实施已授权的后端 A1—A5；没有执行旧 Generate 实施书，也没有再次全量审计。

## IMPLEMENTATION STATUS

**已完成生产修复、隔离离线验收和本地逻辑提交。** 修复了候选事实向审查视图的投影、原 attempt 的补证据续行、父证据递归复制、压缩 producer 协议及累计维护预算、压缩应用可见性、独立退出命令与取消读视图。正式核验、审批、Commit 和 CAS 保持原有权威。

真实模型效果、真实缓存率、供应商费用节省、科学 E2E 均为 **NOT_RUN**。原科学任务仍未恢复，历史 Commit、报告、usage、epoch 与取消事实没有改写。

## ACTUAL BASE HEAD / FINAL HEAD / BRANCH

- 实际 BASE：`de7037f7a522e5a7350c07b6349b80965b8f49ed`，也是复用的阶段 A 审计提交。
- 分支：`refactor/consultant-controller-authority`。
- FINAL HEAD：包含本交付文件的 B3 提交；完整 hash 随最终答复交付，也可执行 `git log -1 --format=%H -- docs/implementation/session_1789471955_phase_b_20260915/README.md` 获取。
- 保留的 Generate 修复：`5bc79f2cc42eccaeb17caad34cea9c23f989c116`。
- 修改前核对目标函数：最终 15 个生产文件在实际 BASE 均无重叠工作树修改。记录见 [baseline.json](baseline.json)、[protection.json](protection.json)。918 个已记录 Python 文件中，903 个保持原字节；所有初始工作树状态条目保留。此哈希覆盖不被夸大为对全部未跟踪非 Python 文件的事后哈希证明。

## A1—A5 STATUS / PRODUCTION FILES AND FUNCTIONS CHANGED

| 切片 | 实际生产修改 | 状态 |
|---|---|---|
| A1 | `evidence_projection.candidate_binding_facts` 从同 task/Plan/step/action/attempt 的 immutable action 复制 value/type/source；`committed_dependency_facts` 只取声明依赖并检查 revision、effect、输入身份、validation/Commit；`semantic_completion._derive_report_facts` 显示候选存在与 accepted 两个事实；`CommitStep.run` 保存后续可引用的 parser/predicate/input scope | 已实施，历史三报告投影和作用域反例通过 |
| A2 | `TypedSupervisor.run` 仅接受既有已接纳 Recovery 回执指向的原 attempt 重新核验；RecoveryRuntime 保存定点读取证据；Reviewer 身份包含新读取事实而不随报告包装 ID 重置；`supervisor.py` 与 Recovery 提示词区分 W、D、真实 concern、补证据与新动作 | 已实施，真实 Review→Recovery read→Review→Commit 接线通过 |
| A3 | `prepare_inspection_evidence` 保存父证据索引而非 body；`InspectionObjectReader.read` 的 `causal_path` 逐级验证授权、hash、scope、revision，按页取回；`recovery_tools`/`recovery_agent` 接入既有 evidence.read；`_normalize_inherited_materials` 只按完整来源身份去重 | 已实施，四包引用和逐级读取通过；历史组件复现限制见下文 |
| A4 | `context_compaction.prepare_compaction` 与层次 map/selector 先检查实际完整 wire、mode 和 protected exact 预算；真实 parser schema 进入 compactor prompt；有效 map 按来源身份复用，跨局部纠正/工具尾部累计成本；`Schema2RecoveryNode.run` 沿用 archive→CAS→下一请求；`archive_recovery_episode` 和 RecoveryRuntime 保存维护账本；CLI 区分 returned/applied/rejected/cancelled 与 fallback | 已实施，合法应用、局部修正、预算触边、取消/CAS 竞争通过 |
| A5 | `BioAnalystAgent.interactive_mode` 在路由前识别完整独立命令；`_run_agent_interrupt_loop` 和 saved `/resume` 审批入口识别相同退出别名；`plan_tracker.set_task_status` 同步 cancelled 兼容视图；输出故障不使退出变任务 | 已实施，子进程与审批入口通过 |

生产文件共 15 个：`agent_core/evidence_projection.py`、`agent_core/semantic_completion.py`、`agent_core/plan_tracker.py`、`agent_core/workflow/{commit_step,verifier,inspection_evidence,recovery_agent,recovery_tools,recovery_node,recovery_runtime,recovery_context,context_compaction}.py`、`prompts/context_compaction.py`、`supervisor.py`、`bio_agents.py`。

## REUSED EXISTING OWNERS / CAPABILITIES

复用 CompletionReport、TypedSupervisor、现有 `revalidation_required`、CommitStep、RecoveryRuntime、InspectionObjectReader、RecoveryAgentLoop、PhaseArchiveStore、PlanState CAS、CLI 输入/审批和 canonical cancellation。没有新增 Controller、纠错 Agent、语义 binder、全局类型配对、外部缓存/RAG 服务。没有扩大业务候选次数或放松 unknown-field、coverage、protected-material、审批及 Commit 校验。

### 审查与科学事实的边界

三份历史报告中的 typed binding 值重新投影后可见，但仍是 `not_accepted`。依赖的旧 Commit 能证明已提交关联事实，旧回执未保存的 parser 正文明确为 `not_saved_in_legacy_receipt`，不会补造 assembly/REF 同一性的证明。新 Commit 从此保存范围与 parser/predicate 事实，以便后继步骤可靠引用。

首轮工作区后置条件失败仍保留；中间阶段实际 config 指向 W、文件位于 D=W/data 的不一致，有旧 `artifact.read` 原文回执；最终 config 明确为 W/data。见 [layout_contract_receipt.json](evidence/layout_contract_receipt.json)。本轮只校正事实展示和原 owner 的提示词，不增加通用路径推断器，也不声称模型已在真实科学场景接受最终布局。

正例的正式接线：同一个 execution attempt，Supervisor concern→Recovery 定点读取→Supervisor 正式核验与审查→Commit；**Execute 0 次、Review 2 次、有效 Commit 1 次**。重复 Commit 不增加 state generation。反例中正式 gate 通过但 concern 未解决，补读后仍为 inspection_required，不能 Commit。见 [phase_b_review_commit.json](evidence/phase_b_review_commit.json)。

## OFFLINE TEST COMMANDS AND RESULTS

全部通过禁止网络及 `.env*`/凭据启动脚本读取的离线 bootstrap 运行，provider 使用显式受控 fixture。实际命令、范围与结果见 [TESTS.md](TESTS.md)。

- 定向验收加 typed Commit/审批作用域回归：**93 passed，4 subtests passed**。
- 最终维护账本落盘补丁后，重跑全部组合接缝：**12 passed**（与前项有重叠，不累计成独立用例数）。
- 既有 Generate、真实 Plan approval→Generate、六种 Planner 取消来源、CLI/审批/输入回归：**80 passed**。
- `git diff --check` 通过。

不声称旧测试全集全绿：探索性旧套件在 BASE 已有 14 失败；早期补丁运行多出 2 个旧人工微小 wire 容量 fixture 的失败，完整 schema 后触发层次路径并要求源归档。新的测试以完整 payload 和真实 archive 验证相应正反例，未为了旧容量假设放松校验。另一个 inspection 旧数量断言在 BASE/补丁均失败。更宽的探索运行被中断，不能当作 PASS。详见 [legacy_test_diagnostics.json](evidence/legacy_test_diagnostics.json)。

## INTEGRATED REPLAY EVIDENCE

接线使用真实生产事实投影、validator、Recovery tools、对象存储、上下文构造、CAS、Commit 与 CLI 分派。外部 provider 回答和已有 Execute 叶子是离线 fixture；这只证明软件控制接线。

| 编号 | 覆盖证据 |
|---|---|
| T01 | `test_facts.py`：三份已存报告、原 action 与 validation，候选值/类型不丢且不冒认 accepted |
| T02 | 声明依赖正例以及 missing、revision、input、undeclared、effect、foreign scope 反例；组合 Commit 后 parser 事实可见 |
| T03 | 首轮原始 attestation 失败；旧 config 读取原文与最终 W/data 分别保留；真实 Review/Commit 接线拒绝未解决 concern；真实模型科学判断 NOT_RUN |
| T04 | 原 attempt 补读续行、Execute 0、重复 Commit 幂等；陈旧 Recovery 回执不能进入 verifier |
| T05 | 既有 approval scope/lineage、陈旧 action/Plan/attempt 拒收、真实 Plan approval→Generate 路径保留 |
| T06 | 四个已存 causal packet 通过生产 publisher 引用化；逐级读回最早 execution；外来 ref/取消后读取拒绝；同文不同来源保留 |
| T07 | 四份历史非法 final plan 仍以原错误族拒收 |
| T08 | 使用生产 dataclass/parser schema，normal 保护材料在 map 前排除；capacity-pressure 和保护材料超容量正反例 |
| T09 | 合法 plan→候选归档→实际 CAS→生产请求 builder 中摘要出现、旧大段消失 |
| T10 | 新工具尾部、revision/origin 改变、CAS 竞争；保护 System/Seed 不变 |
| T11 | 两个非法 final 后新增工具尾部，旧 map 复用、只有新增尾部 map；调用与预留不重置，落盘账本一致 |
| T12 | 0044/0058/0076 原始 request 定点比较；测试按完整 map payload 衡量容量 |
| T13 | 已知 usage 与 UNKNOWN 预留、累计调用/成本/墙钟预算；拒收前有明确预算错误 |
| T14 | 完整输入安全时有限 fallback；不安全时无下一业务请求并走结构化停止 |
| T15 | map/final 取消、候选归档后 CAS 前取消、CAS 冲突、提交后取消；提交后记录保留 |
| T16 | 已保存同 episode/跨 episode/map/final correction/corrective Generate 前缀对照；新请求 System/Seed 哈希稳定 |
| T17 | 13 个实际 interactive 子进程（独立控制及正文反例）；8 个代码审批/saved-resume 入口；审批 parser 不会把 exit 当 approve |
| T18 | cancelled 三个读视图一致，原 attempt/step 保留；重复取消一次回执；UI/日志失败不改变业务结果 |
| T19 | Generate/Plan/CLI/审批/输入 80 项既有回归 |
| T20 | 正例完成单次 Commit；concern 反例不能 Commit；合法/失败/修正/预算/取消的 10 种压缩接缝 |

## BEFORE / AFTER CONTEXT AND CALL METRICS

### 三个超大 map 的定点定位

0044、0058、0076 每条 saved request 的 content 都是 **327,305 字符**，其中同一个原始 source segment body 为 **299,348 字符**，JSON 字符串编码后为 322,558 字符，coverage 都是 full；源引用、正文 hash 与 segment hash 相同。不同 request snapshot hash 来自事务包装差异，不能当作材料不同。见 [targeted_large_maps.json](evidence/targeted_large_maps.json)。

这是单个完整递归 causal-history 对象在宽容量 map 中未被分片、并在后续事务重复处理。外围元数据不是该大输入的主要来源。A3 减少正文，A4 避免未变源重复 map；并未把历史 108906/108897/108900 prompt usage 改成修复后的实测值。

### 因果正文，同一序列化口径

| 因果包 | 历史正文字符 | 新引用正文字符 |
|---|---:|---:|
| episode 2 | 101,790 | 8,035 |
| episode 3 | 212,914 | 7,534 |
| episode 4 | 306,985 | 7,225 |

口径为 `json.dumps(body, ensure_ascii=False)`；它与 provider 内 XML/紧凑 JSON 的字符数不同，不混加。episode 1 没有父因果包。每份原始历史对象保持不变，引用读取每页最多 120 行/16,000 字符，最多 8 层授权路径。

四个 episode 的当前构造器输出、每条 message/source ref/hash、缺项与本地估算标签均在 [four_episode_wire_composition.json](evidence/four_episode_wire_composition.json)。其历史首请求总字符分别为 194453/347860/459551/558696，组件重放为 188799/200344/201213/207411。**这组总数不是纯修复差值**：旧 Plan record hash 不能直接冒作当前图输入；组件重放使用已存 packet 和最终 Plan 快照，旧 failure_source 对部分 validation/report/decision 无完整 body，明确 externalized/unavailable。不能将缺失旧 body 带来的减少算作 A3 收益。上述因果正文表和下述同一受控请求则是可直接核对的同样本比较。

### 压缩接线调用数（受控 provider，不是真实物理账单）

| 场景 | 业务调用 | map | final | 结果 |
|---|---:|---:|---:|---|
| 一次合法应用 | 2 | 2 | 1 | 应用并进入下一请求 |
| final 局部修正后应用 | 2 | 2 | 2 | 原 map 保留，第二份 final 合法后应用 |
| 两个 final 均非法但完整输入安全 | 2 | 2 | 2 | 旧上下文安全 fallback，未激活 |
| 非法 final 后再读新尾部 | 3 | 3 | 2 | 只新增 1 个尾部 map；累计预留触边，未再发 final |
| 完整输入不安全 | 1 | 2 | 0 | 成本边界阻止 final，结构化停止，无第二业务请求 |
| final/CAS 前取消或 CAS 冲突 | 1 | 2 | 1 | 不激活 |
| CAS 后取消 | 2 | 2 | 1 | 已提交 epoch 保留，后续业务取消 |
| 显示观察失败 | 2 | 2 | 1 | 合法应用仍成立 |

完整账本见 [metrics.json](evidence/metrics.json)、[integrated_replay.json](evidence/integrated_replay.json)。map 复用另以“2 个旧源+新尾部”进行直接 production prepare 测试：再次进入仅新增 1 个 map，不重新映射旧源。最终回执和归档保存相同累计调用账本；中途取消/不安全停止的部分现场没有最终持久账本，因此该字段为 null，UNKNOWN 调用数仍按 fixture 实际调用记录，绝不填 0。

维护策略集中在 `prepare_compaction`：64 次调用上限（低于既有层次路径 256 上限）、累计成本/预留上限为当前有效 context limit 的 2 倍、300 秒累计维护墙钟。它们是本轮采用的保守软件维护上限，未由真实模型吞吐标定。2 倍从当前模型/应用有效容量派生，不是统一 200k 或账户额度。调整应用/provider context policy 会改变派生 Token 上限；调用/墙钟内部默认值须在同一 owner 修改并重跑边界测试，不存在隐含环境变量调大预算。业务候选次数与 final 最多两次的局部纠正次数未增加。

成本先按完整 wire 的本地估计加输出预留收费；有正整数 total usage 才替换预留，未知、失败、取消保留 UNKNOWN。集成样本使用 codepoints 估算器施压：243257、375111、380124 等是本地估计/预留值，**不是 provider Token 实报**。

## COMPACTION ACTIVATION + NEXT-REQUEST EVIDENCE

同一 `applied` 样本的真实源归档 transcript 为 **150,507 字符**；实际第二业务请求为 **57,653 字符**。其中 System 与冻结 Seed 的 content hash 在两次业务请求间相同；摘要 `SUMMARY ENTERS NEXT REQUEST` 出现，原 80k 连续 N 正文不再出现。必要当前证据仍作为受保护消息保留。

证据链都已复制成可提交的离线产物：

1. [源 episode 归档](evidence/applied_source_archive_ref.json.gz)：原 provider-visible transcript。
2. [合法候选归档](evidence/applied_candidate_epoch_archive_ref.json.gz)：经过生产 validator 的 plan、覆盖/保护材料、provider baseline 与维护账本。
3. [集成回执](evidence/integrated_replay.json)：durable active pointer、source epoch、source manifest hash、事件顺序，以及每条下一请求消息的 hash/长度。
4. [实际下一请求](evidence/applied_actual_next_request.json.gz)：真实生产 builder 交给受控 provider 的消息值；不是重新手工拼的“预期请求”。

收集器另外验证 archive content digest 与 ref 相等、下一请求 JSON hash 与捕获记录一致。完整 artifact 文件 hash 见 [artifact_hashes.json](evidence/artifact_hashes.json)。

## CONTROL-COMMAND ZERO-MODEL EVIDENCE

`/exit`、`/exit\n`、`/exit\r\n`、首尾空格/tab/换行，以及原有别名：实际 interactive 子进程均退出码 **0**，独立控制输入的 route/model/execute 均 **0**。`Explain /exit…`、嵌入多行正文和代码块保持原文进入正常任务分派；其模型叶子被离线替身替代，不能把正文用例的模型 0 当作生产正文不调用模型。

代码审批与 saved `/resume` 入口的 8 个用例均 **0 次新增图分派、0 model、0 Execute**，取消等待并保留正式回执；回到提示符后的独立退出也被前置拦截。取消 task_status、execution_status、runtime_state.status 一致，已完成步骤和 attempt 未重写。见 [control_commands.json](evidence/control_commands.json)。

新测试退出码不补写事故当时未知的 OS 退出码；本地取消回执不证明远端 provider 停止计费。

## FULL COMMIT HASHES / FILE SCOPE

- B1：`668c1f68fb743d99cf86f85b4372bb673a36bcd8`，审查事实、依赖 receipt、原 attempt 补证据接线与测试。
- B2：`3faac12b9fc7655cec802653367b319b8ac31d35`，父证据引用读取、source identity 去重、compactor producer/严格校验、map 复用与维护成本。
- B3：本 README 所在提交，控制/取消修复、维护账本持久化与活动显示收尾、组合验收、保护记录及本交付证据。完整 hash 见最终答复。

仅显式暂存本轮文件，提交前检查 staged diff、秘密模式及 gzip 解压内容，不包含凭据或无关工作树变更。没有 push，没有切换或重置历史 HEAD。

## REMAINING GAPS / NEXT AUTHORIZATION NEEDED

- **LIVE MODEL / REAL CACHE RATE / SCIENCE E2E / BILLING SAVINGS：NOT_RUN**。模拟接受只证明软件接线；不能保证真实 Reviewer/compactor 的科学判断或摘要质量。
- 四个历史 episode 的旧 Plan record 与部分 body 的缺口、旧 Commit 未保存的 parser 正文、历史未捕获 loaded_revision 范围及两条未知 usage 继续保持未知；没有真实重跑补证据。
- 旧微小容量/固定数量/旧 CAS report-ID 假设的测试没有在共享工作树上擅改，探索性失败单列。本轮关键边界有新生产接线正反例，不能据此宣布全仓测试全绿。
- 已授权范围到此停止。真实模型验证、原科学任务恢复及额外旧测试整治需要新的具体授权；当前未启动这些工作。
