# GWAS 故障增量修改计划（尚未实施）

审计基线是 `82c27867d862ef32d657fbc6162c70d4bc06492c` 加保留 overlay，当前 905 份源/测试与上次最终交付清单一致。实施时重新检查 `evidence/baseline_current.json` 和 `version_comparison.json`，不能仅 checkout 该 commit 后宣称具有相同前提。发现、证据等级及现场边界见 REPORT.md、EVIDENCE_MATRIX.tsv 和 notes/。

本计划仅形成可审阅的未来改动。原 session、生产文件、环境及注册表本轮均不修改。以下测试通过后才形成各自生产逻辑提交；真实模型、真实安装、原任务恢复及完整 GWAS/WGS 均不包含在本轮授权内。

## 顺序和共享文件所有权

| 切片 | 目的 | 顺序与并行 | 独立提交建议 |
|---|---|---|---|
| S5 故障事实和显示 | 先让验证错误、Recovery 停止与交付可辨认 | 可与 S4、S1 的独立模块开发并行；与 S4 共享 recovery_node.py 串行集成 | `fix: preserve validation cause through recovery and terminal reporting` |
| S4 Recovery 请求预算 | 修复字符门、归档正式回读和输出预留不一致 | 与 S1 并行；先确定正式可读证据，再处理初次和后续预算；共享 bio_agents.py 的 compactor 输出配置由同一集成人合入 | `fix: budget complete recovery requests before dispatch` |
| S1 候选发现与验证 | 让注册位置成为可验证候选，按阶段检查 | 环境服务层先完成；保持 Installer 精确授权目标 | `fix: discover registered runtime candidates without cache dependence` |
| S2 事实追加与执行消费 | 让新事实进入实际下一请求及批准动作 | 依赖 S1 的事实接口；与 S1 同一 bio_agents.py 集成人，不能分别改同一函数后拼接 | `fix: deliver runtime observations to phase context and actions` |
| S3 输入观察与阶段阻塞 | 先看结构再对齐，保留未知 Q 映射 | 依赖 S2 传递观察的接口约定；Skill/helper 可独立准备，最后集成 | `fix: inspect GWAS input structure before sample alignment` |

S5/S4 先提供可信诊断和可工作的恢复入口；S1/S2 共同闭合环境发现到执行；S3 防止即使工具找到了仍产生错误样本输入。五者都完成针对性交叉验收后，才具备请求恢复科学任务的基础。不能把“先解除 32k”当成此次问题全部解决。

## S1：PackageManager 负责候选发现，当前操作负责验证

**对应发现与变化。** ENV-01/ENV-03/ENV-04/ENV-06：配置被加载，但默认环境失败且 `use_cache=False` 时跳过候选搜索（既有缺陷）；Installer 原候选入口删除且同条件无等价替代（本轮功能回归、非此次直接原因）。修复后禁用缓存意味着重新查证，不能关闭发现；候选可来自既有正式索引，但只有针对选定阶段的成功探测能升级为可用。

**文件、函数、接线。**

- `package_manager.py`：复用 `find_matching_environment()`、环境索引/登记读取和 `verify_environment(..., persist=False)`；由 PackageManager 统一提供候选。若当前单个最佳匹配接口不足，在同类中增加有界的候选枚举方法，原方法作兼容单候选入口；不建立另一份全局 registry。先复用生产已读取的软件 registry、配置 Conda 根和登记 source。审计枚举的 ~/.conda/environments.txt 并非现有 PM 已接管；若加入该有界索引，必须单列候选来源。外部 mamba/任意 prefix 目前不属于 Inspector/安装工单支持范围，只报告候选待支持，不能自动登记为已验证可用。不进行全盘扫描。记录匹配的是工具、包、语言还是能力，避免将部分命中说成整个 workflow 就绪。
- `bio_agents.py`：调整 `_runtime_audit_for_skill()` 中 `if not use_cache` 的位置和语义；`_consultant_runtime_preflight()` 保持 fresh/scoped 要求，但允许发现并重新验证候选；`_verify_conda_env_presence()` 的目录结果不能替代实际工具/子命令结果。`_inspect_declared_runtime()`、`_verified_runtime_paths()` 与当前固定探测编译器仍是实际检查和路径记录来源。
- `agent_core/skill_manager.py`：保留 `get_runtime_env()` 已实现的成功 Skill 登记优先；保留 `get_runtime_requirements()` 的明确目标优先。验证前登记位于 `bio_agents.py::_verify_skill_runtime()` 调用 PM.register_environment；失败结果写入 SkillManager.record_environment。检查这些入口的 status，明确 `registered` 只表示记录。不要把发现过程的持久写入伪装成只读；验证和正式登记仍在原授权/提交点。
- `bio_agents.py::_publish_runtime_dependency_question()`、`_ask_installation_resolution()`、`_install_subagent_impl()`：缺少明确目标时向已有咨询/恢复选择流程提供同一候选服务的结果；有明确 request target 时严格匹配，不擅自改目标。实际安装继续复用 现有 `installation_request` / `installation_receipt` 字典合同、授权消费、取消与目标复验。不要恢复旧 Installer exact-match 分支里推进科学 Plan current 的逻辑。

**复用与边界。** 候选、验证可用、已选定、已批准四种事实继续分离。复用 runtime evidence ledger、现有环境/解释器路径形式、固定 argv inspection_probes、安装后双登记及 generation epoch/supersedes。按阶段可用不同环境；只验证 bcftools 不赋予 EMMAX/R/LD 工具可用性。保留同 prefix 依赖准备、取消、授权目标哈希、旧 binding 失效和 `_not_dispatched` 保护。

**兼容与 schema。** 首选调整内部调用和已有 observation/evidence 字段，不新增动作 schema、环境路由器或安装权威。旧 `registered` 记录仍可读，不能自动改成 success。现有显式环境请求优先级不变；只读发现不安装、不注册。无科学依赖的通用动作不增加 binding 门槛。

**交集与回归。** 与上次合并计划的 R2/A1/A2、Installer target/双登记及同 prefix preparation 重叠；复用其组件，仅补候选入口。复用 `tests/test_runtime_preflight.py`、`tests/test_runtime_evidence_transport.py` 的相关边界，并按现行 Planner 协议补当前通路，不直接把旧失败夹具迁回。结合 `tests/test_unified_dependency_preparation.py`、`tests/test_unified_runtime_graph.py` 的已测身份/授权链。

**验收。** 在相同需求、注册表和探测结果下，默认不存在/缺包/启动失败/版本不符分别取得具体事实；`use_cache=False` 仍搜索并重新验证；registered-only 不成为 AVAILABLE；多个阶段分别返回 ready/blocked；无目标 Installer 能提出已有候选，有目标时不得换；取消及目标不匹配不执行安装。真实已有工具的只读版本/样本查询只验到该操作，不作为关联验收。

**提交与回滚。** 提交上述 PackageManager/SkillManager/必要 bio_agents.py 函数及对应测试；不提交 registry、.env 或机器环境。若行为回退，只 revert 本切片提交，保留已实施 binding 和安装权限控制。S2 依赖的接口必须连同消费方一起回滚或保留兼容壳。真实安装收益和主机隔离能力未由本轮证明。

## S2：追加新事实，并让批准动作消费所选工具

**对应发现与变化。** ENV-02/ENV-03/ENV-06：后台已有 audit，但重建 seed 被冻结的首 seed 替代，后续 provider 只见通用 Skill 接受回执；GWAS 通用动作没有选择并消费其他环境。修复后新证据进入下一实际请求，保持来源、阶段、版本和未知项；动作依赖由显式选择/正式能力声明而来。

**文件、函数、接线。**

- `bio_agents.py::_build_graph()` 内 `consultant()`：在 Skill 选择后的 `_consultant_runtime_seed_evidence()` / preflight 返回点，将实际检查结果通过已有 phase observation/event 接口追加；不能只赋值 `LLMPayload.feedback.runtime_checks` 后重建被冻结 seed。选择分支目前返回 `runtime_audit` 旁字段及通用消息，需在此补消费接线。
- `agent_core/workflow/phase_subgraph.py::PhaseProviderContext.queue_provider_event()` / `prepare_messages_with_compaction()`：复用现有事件去重和追加逻辑；生产者提供已存在的证据/请求/epoch 身份。相同证据重入只追加一次，不同版本允许新事件，并保留 supersedes。不要每轮刷新 System、替换首 seed 或重建全部历史。本轮已用实际 builder 见证旧缺口和现有 event 接口可用。
- `bio_agents.py::_runtime_binding_source()`、`_consultant_runtime_seed_evidence()` 、`agent_core/node_context.py::_compact_planner_runtime_projection()` 与 `prompts/manager.py` 的 Consultant 模板：以同一 ledger 事实提供候选/验证状态及引用；handoff 与 Plan 相关事实复用现有材料继承，不复制全局软件清单。`NOT_REQUIRED` 只表示所选正式能力没有必需 binding，不能解释为完全无软件依赖。
- `bio_agents.py::_generate_impl()` / `_bind_pending_action()`：消费当前阶段明确选中的调用形式。已声明 runtime IDs 的能力继续走 `agent_core/action_runtime.py::resolve_action_runtime_bindings()`；合法通用动作可使用经验证的显式绝对路径、action environment 或已验证 ambient PATH。不得扫描自由脚本反推完整依赖、替换命令或新增重复 binding 权威。
- 科学依赖的通用初检若复用现有 Skill，可在 Skill 中为该明确操作声明必要依赖，或选择不需科学工具的 header 读取。模型缺少所选工具事实时返回既有诊断/修正入口，不能由空 manifest 单独判断错误。
- `build_exact_action_environment()`、现有 action 审批身份及 runner：检查批准候选中的 argv/cwd/env/工具身份与实际进程一致；工具失效走已有候选修正、重新检查/审批，不在批准后注入 PATH 或切换环境。执行后依赖失败通过现有 failure evidence 和独立 Recovery 工具读取恢复。

**兼容、边界和交集。** 不增加新的全局事件 schema；优先使用 `queue_provider_event` 和现有 typed observation。保留私有阶段 seed、Plan 单一权威、action immutable binding、回滚失效、helper 调用和 generation claim。与上次合并计划的环境事实传递及真实 tiny graph 重叠，补的是 producer→实际 provider→通用阶段消费者，不能重复实现 ledger/resolver。

**验收。** 首 seed 冻结后新的失败/候选确实出现在下一 request；同一证据重入一次、新验证可覆盖旧结论但保留历史；完整捕获请求而非仅检查 state。经选定候选→合法候选对象→原审批→真实临时只读进程核对 argv/cwd/env/path/输出；中途替换工具必须拒绝并重新修正。另跑无外部依赖的 Bash/Python 和只靠已验证 PATH 的合法 empty-binding 动作，防止机械增设门槛。

**顺序、提交与回滚。** S1 接口稳定后合入，S1/S2 的 bio_agents.py 由一名集成人串行处理。提交 producer 接线、必要投影和最少相关测试；不调整整个 phase/controller。回滚本切片恢复旧事件消费，不回填旧 manifest、不删除证据；若与 S1 同批部署，按依赖逆序回滚。主机受控 inspector 的隔离失败仍是独立限制，审计中手工组装合法候选到进程不等于自动模型主链成功。

## S3：结构观察优先，未知 Q 映射保持阻塞

**对应发现与变化。** I1/I2/I3/P1：handoff/Plan 过早假定 Q 存在 ID；Generate 无观察跳表型首行并取 Q 数值首列；缺 bcftools 写空样本文件；Planner 协议拒收本身合理。修复后初检先产生结构事实，对有可靠 ID 的输入对齐，对未知 Q 保留未满足条件，依赖缺失不伪装结果。

**文件、函数、接线。**

- `skills/population-genetics/gwas-vcf-prep-qc/SKILL.md` 与 `popgen-gwas-workflow/SKILL.md`：明确前置结构观察和 Q 来源要求；初检产物/observation 根据实际操作声明，不能无条件要求“从无 ID Q 提取 ID 文件”。任务级报告继续有覆盖，初检不得承担虚构结果。当前科学实现选择若改变，使用既有 Plan revision；单纯读取结构不强制改 Plan。
- 复用并改进 `skills/population-genetics/gwas-vcf-prep-qc/scripts/sort_phenotype.py::read_table()`、`resolve_trait_indexes()`、`main()`：提供可表达“结构未定/无 ID/需映射”的检查结果；保持已有无表头支持，避免重复 ID 被 dict 静默覆盖，避免未经确认对齐失败后全 NA 仍成功。列选择应来源于已观察结构或明确参数，不能以本例样本数/列名硬编码。必要的新检查函数位于同一 Skill/helper，不放入全局语义控制器。
- 轻量 VCF header 检查可在现有 Skill 增加受限标准库 helper/明确操作，或使用 S1 已验证 bcftools；只读到 header，记录读取范围，不假称完整文件验证。具体候选和依赖仍由现有 Generate/审批处理。
- `prompts/manager.py` 的相应 handoff/Generate 规则、`bio_agents.py` 现有 `consultant()` / `_generate_impl()` 消费观察：无实际结构事实时安排初检，不能把未决槽位同时当成已成立前提。现有 `agent_core/consultation_state.py::inspect_resource()` 只返回 stat/可读性，不能声称它已经读取表内容；可保留其元数据职责，内容观察优先由所选 Skill 的合法检查动作产生。
- 保留 `agent_core/managed_artifacts.py::reconcile_runtime_outputs()` 对本例空样本文件的拒收；明确样本文件的非空语义，并兼容合法空文本。不要为绕过 `OBSERVED_ARTIFACT_CONTENT_INVALID` 扩大 file_exists 空文件豁免。通过 S5 让核验失败精确显示。

**兼容与交集。** 以现有 StepEffect observation 和 Skill 参数/能力合同实现，不添加“所有检查必产文件”的协议。helper 参数扩展保持原明确输入兼容，新增严格行为只覆盖输入身份确实不足的情形。若修改 Skill 能力内容，更新对应版本/哈希并通过正式新 Plan 接纳；旧批准记录不可原地回填。上次 helper 调用及 binding 修复保留。

**验收。** 无表头不丢首样本；带表头按实际列解释；重复/缺失/错序 ID 有明确诊断；数值 Q 不当 ID，行数一致仍为 unknown；提供可信映射后按 VCF/TFAM 对齐并记录来源；读取依赖不可用时不产空必需列表冒充成功；允许标准库检查的阶段无需科学 binding。现有 ask_user 效果三种候选的编译边界和通用只读无文件 observation 均保留。使用正式输入只读验证本例，用合成边界输入做软件回归，禁止拿测试数据产生本任务科学结果。

**提交与回滚。** S2 观察结构稳定后提交以上 Skill/helper、最少提示接线和相关输入边界测试。冻结脚本本身是历史证据，不修改。回滚切片及其 Skill 版本引用，旧科学执行保持未恢复；不能用旧解析器运行尚未明确映射的 Q。此切片不提供 Q 真实顺序、显著性阈值或 GWAS 科学结果。

## S4：完整请求的首次准入、后续增长与可读引用

**对应发现与变化。** R1/R2/R3/R4/R5：32,097 字符 seed 在 32,000 字符门提前退出；完整请求和 token 维护尚未开始。除此之外审计检查了首轮继承材料、工具定义、单次巨大返回、压缩请求自身和证据读取。修复必须覆盖整个生命周期，不能只消掉 97 字符或把 32000 改为 200000。

**文件、函数、接线。**

- `agent_core/workflow/recovery_agent.py::project_recovery_seed_for_provider()` / `recovery_seed_capacity()` / `RecoveryAgentLoop.ainvoke()`：将 seed 字符量保留为组件遥测；在有效完整预算允许时不以旧字符阈值独立终止。继续复用现有单对象大小、读取范围及身份/协议限制，不能另造一个较小字符派发门。
- 同文件 `_context_budget_snapshot()`、`RecoveryAgentLoop._graph()` 内 `call_model`、工具观察装配：统一使用 `measure_provider_input()` 和实际模型输出预留；计入 System、固定 seed、允许继承材料、当前失败、局部消息、工具 schema 和返回。每次工具结果、观察、重试后在实际 provider 调用前再次计量，输出 token 上限与 reserve 必须同一有效配置。
- `agent_core/workflow/recovery_node.py::_initial_inherited_materials()`、`_build_compaction_request()`、`_invoke_compactor()`、`_run_investigation()`：接入并保留已见证有效的首次及后续预算拦截/维护。修的是其前面的 seed 字符门及材料可读性，不能重新实现已有首次大继承材料处理。本例94,760估算输入无需先压缩。只有完整计量证明有必要时，对相同身份/版本/权限重复投影做可选表示优化，或将长详情转已验证可读的同scope证据；极端必要固定事实仍超容量时准确停止。保留独立 Recovery System 与本阶段材料；不引入 Generate 全历史。
- `agent_core/workflow/context_compaction.py::ContextBudgetPolicy`、`measure_provider_input()` 和现有 compactor：复用计量、source units、coverage、epoch activation 和重启恢复。保留现有 compactor 的有界 source-range 分段、selector、coverage 验证和维护额度；本轮已见证其可处理大首轮材料。对每个分段的独立准入统一真实输出预留，不能让 adapter 额外允许输出而预算仍少预留；合并摘要后继续复用现有再计量。
- `bio_agents.py::_ainvoke_llm()` 的 context_compaction 分支：核对默认 `max_tokens=16000` 与当前 reserve 8192 不一致，统一有效输出策略或依据真实上限计算 reserve。不要靠修改全局常量掩盖 provider 特定配置；实际 SDK/服务容量无法证实则记录配置来源和限制。
- `recovery_agent.py::_inspection_manifest()` / `_failure_projection()`、`agent_core/workflow/recovery_tools.py` 与现有证据存储/reader：元数据只保留一份但不丢作用域；详情采用已有持久、授权、版本锁定 ref。巨量工具结果若外置，其返回 ref 必须能被当前 dispatcher 重新读取，且新进程可重建权限。不要将仅存在于临时目录的路径作为已持久证据。

**固定最低事实与字段去向。** 固定保留 task/Plan/step/attempt/approved scope 身份、必要限制、rc=0、验证失败码及目标、未 Commit、已采取动作/未解决问题、停止触发；Q 映射仍未知。inspection_manifest 的 27 项是不同引用，不能全合并；同一条目的 readable/allowed_tools/source_object_id 等顶层与 scope 重复只留规范一份，并兼容 reader。若计量后确需精简，failure_packet 的 result_realizations 保留失败文件和 reconciliation 摘要，完整 declared/observed 清单可转本轮已证实可读的同scope step_attempt 等正式对象；不能指向 opaque execution ref 或三个未产生正文的占位。完整 TaskSpec/Plan 若已按身份继承只保留一次；不能由路径相同就合并不同版本或权限。详见 notes/recovery_budget.md 的字段级表。

**兼容、归档和交集。** 首选 provider 投影/请求策略改变，不改变冻结 Plan、failure record 或既有证据身份 schema。若需补 reader 对已归档返回的登记，使用当前 scope/identity 机制并保留旧引用读取；无法安全兼容时给显式版本迁移方案。与此前 deferred receipt 精简仅交集于造成此 seed/请求增长的对象，不做全局协议精简。必要事实仍容不下、压缩失败或没有进展时有限终止，保留准确阶段、量、单位、上限和证据入口，无静默截断或无限重试。

**验收。** 32,097 字符但总请求在预算内能进入调用；小 seed/大继承材料在首次调用前拦截并维护；单个大 tool result 在下一次调用前处理且可取回；首次/后续都计工具 schema 和输出预留；compactor 自身不超限；压缩后的 rc=0/空文件/核验拒收/Q未知可恢复；引用跨进程重启可授权读取；失效引用、必要材料仍过大、压缩错误均有限且准确停止。复用 `tests/test_recovery_compaction_production.py`、`tests/test_recovery_context_bounds.py`、`tests/test_recovery_agent_seed.py`、`tests/test_phase_context_compaction_runtime.py` 的相应生产路径，更新旧字符边界断言为新契约，不删除资源保护断言。

**提交与回滚。** 提交本切片的 projection/预算/正式 reader 接线和定向测试，不迁移所有节点。S5 先提供或同期提供准确停止文案。回滚预算切片不删除已归档原始证据；新引用 reader 的兼容支持应保留到依赖记录可读取为止，不能简单删除 schema 后留下不可读恢复历史。本轮离线捕获不能证明模型能成功恢复分析。

## S5：保留原始核验原因、Recovery 停止和交付事实

**对应发现与变化。** F1/F3/F4：进程 rc0、artifact invalid、Commit0；当前链先产生含混执行失败叙述，再将 failure_code 漏掉而回退 EXECUTION_FAILED；容量停止和报告交付是后续独立事件。前两处在 A/current 真函数见证中均复现。修复后四种状态独立、准确，继续使用当前因果记录和终止投影。

**文件、函数、接线。**

- `agent_core/workflow/execution.py::TypedExecute.run()`：区分已启动命令非零失败与 rc=0 的正式 reconciliation 失败；可保留现有 `execution_failed` 路由 envelope，但 code/message 必须携带 validation 事实，不能写 “A launched command failed”。
- `agent_core/workflow/recovery_projection.py::_failure_identity()`、`build_causal_failure_record()`：先消费同一 source/attempt 的 execution_record.failure_code 和结构化 validation/reconciliation 拒收；明确 validation false 但没有具体码才回退 VALIDATION_FAILED，无更具体原因才兼容 EXECUTION_FAILED。保持 cause identity 与生命周期关系，不让 Recovery 容量错误覆盖原 validation failure。
- `agent_core/workflow/inspection_stop.py::_execution()` / `build_inspection_stop()`：消费同一 attempt 的 `producer_failure_index.validation_acceptance` 和目标指针，避免已有明确拒收却显示 unknown；不扫描自由日志或其他 attempt。`bio_agents.py` 的本次 snapshot 已保存 VALIDATION_FAILED，后期 fallback 也取得具体 failure_code，因此 S5 无需修改该大闭包。
- `agent_core/cli_lifecycle.py::_cause_text()`、`_stop_text()`、`terminal_text()` 及现有 stop/report 投影消费方：进程退出0、核验失败、当前步未提交、恢复入口 seed 超限、文件交付成功分别展示。容量原因显示“固定 seed/字符/当前量/上限/首次调用前”即可，无需刷全部遥测。旧记录缺字段时显示未知或按现有详细证据呈现，不捏造恢复次数。
- `agent_core/workflow/recovery_node.py::_run_investigation()` 的局部 `fail` 与 `_inspection_stop_result()`（同 S4 集成）：提供 `terminal_error.failure_stage/detail` 和当前计量。`managed_stop.py::build_managed_stop()` 仅在阶段证据明确时表示本次准入前0调用，不能从默认缺值推导整个 session 累计0。`runtime_recovery_reporting.py` 的 publication/ownership 保持现状，列为回归而非必要生产改动；当前三文件交付是真的，不能套用旧事故归因。

**兼容与交集。** 不另建全局 error schema 或路由框架。现有 `origin` / exact failure_packet 与 `stop_reason.error_code/detail/failure_stage` 分别承载原始原因和停止触发；旧 immutable packet 不改写，读侧仅能按同作用域结构化证据补充解释。缺字段的旧记录只读兼容。保留此前修复的终态 current step、交付路径、取消/安装目标、helper diagnostics。CLI 的普通非零退出和其他 validator 失败都回归。

**验收。** 真实 TypedExecute/recovery projection 以现场记录和隔离观察复现 rc0-validation失败，未来各层 code/message 不再变为进程失败；非零退出仍准确；Recovery 入口失败后 origin/stop_reason 同时保留；报告成功时仍显示科学未完成及0Commit；缺证据时明确未知。对现有报告仅核字节/持久回执，当前 device 与历史不同不得被推导为 writer 重入通过，也不允许为验证改写原文件。

**提交与回滚。** 提交这些有限映射/文案/测试，与 S4 的容量结构字段约定同步。独立 revert 不回滚上次终态/交付组件、不改旧报告。真实现场运行源码整体身份仍有证明边界，但旧/新源码相同函数的隔离见证足以决定最小修复位置。

## 共同集成验收与未授权项

每个切片先跑其有效行为，再做相交链：S1→S2 的候选到实际请求/批准/runner，S2→S3 的结构事实到阶段决策，S3→S5 的空必需产物核验拒收，S5→S4 的准确 failure 到首次/后续 Recovery 预算与可读证据。保留已实施的 binding rollback/stale、安装取消/授权/目标、helper interpreter、Plan 单一权威、审批完整对象及 CLI 终态专项。使用同一累计冻结源码运行，不拼接不同快照通过数。

实现前重新确认 dirty/overlay 和 staged 状态，记录函数级范围；使用隔离候选和精确路径/分块暂存，禁止全量 add/reset。每个切片测试后独立逻辑 commit，最终累计验证后再提交集成修正；不 push。失败时按依赖逆序 revert 对应任务增量，保留用户原有 overlay 和不可变证据，不以清空状态或回填旧 binding 获得通过。

仍未授权/未证明：真实模型对新协议的行为、真实安装及共享环境修改、主机 bwrap 隔离能力修复、原 session 恢复、Q 的真实映射、科学阈值/背景决策和完整 GWAS/WGS。软件离线通过之后，这些仍需要各自明确的后续执行范围；不能把本轮审计或将来 tiny 进程通过写成科学端到端成功。
