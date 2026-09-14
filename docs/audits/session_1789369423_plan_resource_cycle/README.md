# session_1789369423：Plan 资源绑定振荡增量审计

本轮结论：**当前公开协议与选中 snpEff Skill 之间存在具体表达缺口，不能交付通过完整准入的合法 C。** 提交了保留 contig observation → 新工作区 prepare → 同工作区 build → annotation → ANN summary → 正式报告交付的紧凑 C；它仍被同一生产准入链拒绝。不是把“未搜索到 C”当成无解证明：下面给出两组直接相冲突的规则、实际实参和对照。

**PRODUCTION FIX NOT IMPLEMENTED。这里的 PASS 是缺陷/边界 witness PASS，不是 FIX PASS，也不是科学流程成功。** 没有修改生产代码、正式 prompt、Skill 或原有测试断言；没有恢复原 session，没有执行模型/API、科学命令、安装、push。

## 证据层级与版本

| 分类 | 内容 |
|---|---|
| TERMINAL_FACT | 用户给出的七次可见拒收、绑定/删除交替及主动取消。终端只显示部分错误，不等于完整 validator 回执。 |
| VERIFIED_FACT | 七份候选回执按 call ID 配对；全部实际下一请求的反馈 JSON 与落盘 provider_feedback 精确相等；两个资源协议缺口在隔离生产入口重现。 |
| ENGINEERING_JUDGMENT | 框架给出局部正确但无法组合执行的纠正说明，能力角色与资源实例缺少公开关联，是本次振荡的重要机制；不能仅归责模型。 |
| PROPOSED_FIX | 末尾列出的现有声明/编译、结果覆盖、Planner 收敛和取消边界切片；均未实施。 |
| UNKNOWN | 历史完整 loaded_revision、未录制的逐函数内存、全部工具/OS 子进程清理、最早引入提交、真实模型修正成功率。 |

- 开工 branch：`refactor/consultant-controller-authority`；**HEAD：`d1f3779407e891d6fadf1778cd8e0ce7804b1630`**。当前 tree：`a3270220a1a3778b77a8abaf8c0e7a3d74c18e86`。
- 提交前并行任务将 HEAD 推进为 `08a65a423830b4f366845b8910074d7956fd1f4a`，新增范围仅为 `apps/remote-workbench/` 与 `docs/implementation/remote_workbench_20260914/`，共25个路径。本轮只核对该范围；相关生产源码、Skill 和已有测试依赖与冻结导出仍逐字节一致。因此测试基线仍明确登记为上述 d1f/current-overlay，提交父版本单独记录，不冒称重跑了另一组源码。
- 开工 442 项 porcelain 状态，index 无 staged diff。原有五份安全源码 overlay 单独保存在 `preserved_overlays.patch`；配置目录只记录 Git 状态，不读取内容。
- 历史 manifest 的 `disk_head_at_capture` 也是上述 HEAD；`loaded_revision=UNKNOWN`，不能据 disk HEAD 改写成已知。历史 `bio_agents` 没有 loaded-module callable 回执。
- 历史捕获的安全源码与本轮当前对应源码 hash 一致。进一步用同版本 Python 3.12.12 **只 compile、不执行**捕获源码，保留历史 co_filename：资源编译、StepEffect、dataflow、结果覆盖、失败记账等八个核心函数的 marshal_v2 digest 与历史 live callable digest 全部一致，见 `loaded_callable_check.json`。
- 实际选中/继承快照：`skill-snapshot-77ea99a61ece793ee310`，Skill `genomics/snpeff-annotation`，内容 SHA-256 `458a6d5648b0127ef8283e21af917af4bc4e5980a0355c9bd2235dc8e684ac26`。保存快照、请求继承材料、当前 Skill 内容一致。manifest 的空 `skill_versions={}` 不被误写成版本已完整登记。
- 实际候选 metadata：`structured_plan_v2`、`resource_contract_version=2`、`result_contract_version=1`；epoch 记录 `planner_response_protocol_version=2`。第一请求 system 已明确新资源 OMIT resource_binding。精确消息位置、字符 offset 和摘录见 `public_protocol_excerpts.json`。
- 正式路径根由安全 TaskSpec/manifest 证据确认为 `/public/home/liusiqi/03.毕设`，不是 `Biomind_v1`。`task_declarations.json` 保留原 `Part2.vairant_annotation` 拼写和正式输出根；未打开输入内容、未创建正式目录。

## 逐候选循环链

所有索引均为 **0-based JSON/message 索引**。`candidate_01.json` … `candidate_07.json` 是正式 Plan 字段投影，剥离 public_message；原始完整候选字符串 hash 单独保留。不能按 raw hash 去重掉不同调用。

| 轮 | call ID 后缀 | 本轮关键内容 | 完整错误对象数 | 下一实际请求反馈位置 |
|---|---|---|---:|---:|
| 1 / I | `0007-411a4350e98a` | 工作区新声明，无 binding；prepare/build 用 plan.snpeff_data_dir；另有两个无能力步骤 | 9 | message 4 |
| 2 / A₁ | `0008-63590fb2aa4e` | resource_binding.resource_ref=artifact.snpeff_data_dir；mutation 仍用 plan.snpeff_data_dir；合并只读检查并补 evidence_contract | 1 | message 6 |
| 3 / B | `0009-851a710ddc5a` | 删除工作区 binding；mutation 保持 plan.snpeff_data_dir | 5 | message 8 |
| 4 / A₂ | `0010-9a43af4b8e61` | 恢复 binding，且两条 mutation.workspace_ref 改成 artifact.snpeff_data_dir | 1 | message 10 |
| 5 / B | `0011-10a3c7bbeb12` | 删除 binding；两条 mutation 改回 plan.snpeff_data_dir | 5 | message 12 |
| 6 / A₂ | `0012-fca554a2ddf9` | 与第 4 轮原始字符串完全相同 | 1 | message 14 |
| 7 / B | `0013-b12c2f5a1826` | 与第 5 轮原始字符串完全相同 | 5 | message 16 |
| 8 | `0014-9342f9006cba` | 已 dispatch，收到 caller cancellation 回执，无候选正文 | — | — |

第 3/5/7 轮正式候选 hash 相同；3 与 5 的 raw hash 差异仅在 public_message。第 2 与 4 轮不是完全相同：mutation 引用真的从局部引用变成了 capability 角色引用。`candidate_matrix.tsv` 与 `evidence.json` 给出全部 call/attempt/occurrence/scope/epoch/hash。

实际计数：8 次 Planner 请求/transport attempt，7 次正常候选、5 种不同 raw 正文、4 种不同正式 Plan；同一个 planning epoch。7 个逻辑准入轮次共产生 **27 条完整错误对象**。成功接纳/审批/Generate/科学 Execute 都是 0。总 request manifest 14 个：router 1、Consultant 5、Planner 8；metrics 的 13 次 LLM 统计是正常完成调用，不含取消的第 8 次 Planner。transport attempt ordinal 均为 1，没有发现 transport 重试；sdk_max_retries=1 只是配置，不当作实际多调用。

原事故没有逐函数入参逐阶段录制。保存了实际 controller_binding.semantic_handle_map、编译后的拒收候选/receipt 和完整反馈，可以确定各 checker 的资源视图；**不能声称这些等同历史每个函数的内存快照**。本轮 `admission_replay.json` 另记录新合成状态中真实生产函数的 args/kwargs/result。由源代码顺序和已保存编译回执，可重建 7 次首道结构/身份检查、4 次 StepEffect 和 dataflow 编译；完整 validator 函数调用总数未有独立历史计数。

### 每类错误具体指向

1. `PLAN_RESOURCE_REFERENCE_NOT_DECLARED`：第 2/4/6 轮 `$.proposed_artifacts[1].resource_binding`。`agent_core/plan_bindings.py:compile_plan_resource_bindings` 严格分支只查本次 context 的正式 input/output authority；里面没有 `artifact.snpeff_data_dir`。`actual={resource_ref: artifact.snpeff_data_dir}`；expected 明确是“现有已声明 handle，若 NEW 则省略 binding”。它不是 `execution_capability_id` 本身，因此这里返回 NOT_DECLARED，而非 WRONG_DOMAIN。
2. **第一条 STEP_EFFECT**：首轮 `$.steps[3].step_effect`，后续 B/C `$.steps[1].step_effect`，对应 `prepare_database`；内部码 **STEP_EFFECT_CAPABILITY_MISMATCH**。实际 workspace identity 是 `candidate:<本轮scope>:plan.snpeff_data_dir`，expected 是 Skill 的 `artifact.snpeff_data_dir`。
3. **第二条 STEP_EFFECT**：首轮 `$.steps[4].step_effect`，后续 B/C `$.steps[2].step_effect`，对应 `build_database`；内部码相同，涉及同一个工作区的后续有序变更，绝非同一错误重复打印。产生处为 `step_effects._unsupported_explicit_effects → compile_step_effect`。
4. **两条** `PLAN_RESOURCE_BINDING_MISSING`：B/C 的 `$.steps[1].step_effect.workspace_mutations[1]` 与 `$.steps[2].step_effect.workspace_mutations[1]`。effect 编译在报 mismatch 后仍合并显式候选实例与 Skill 的角色 effect；每步形成两个不同 identity 条目。dataflow 查其中遗留的 `artifact.snpeff_data_dir` 无绑定而报错。它们在终端被压缩了；不是新声明本身未生成候选身份。
5. `REQUIRED_OUTPUT_UNCOVERED`：affected output 是 **plan.snpeff_data_dir**，并非 annotated VCF 或 ANN summary。工作区 required=true 被结果覆盖逻辑要求有 `outputs` producer，实际这里只有 MUTATE。
6. 首轮另有 `ACTION_CAPABILITY_SCOPE_MISSING` 两条，定位 `inspect_inputs` 与 `resolve_runtime` 的 `execution_capability_id`；还有它们各自的 `PLAN_STEP_EFFECT_UNSPECIFIED`。合并首轮实际共 9 条。补 evidence_contract 并不是这两条 STEP_EFFECT 的内部错误解释；历史明确内部码是 capability mismatch。

A 轮在严格资源校验即返回，**后面的 StepEffect/dataflow/结果覆盖未检查**。不能因为 A 轮未显示它们，就宣布这些约束修好了。B 轮检查到的 correlated errors 有完整字段证据。

### 谁要求删除 binding，反馈是否丢失

第一次与第二次删除前的实际 validator 反馈都明确包含“omit resource_binding for a NEW resource”；第一请求 system 和同源 requirements 也这么说。因此“删除”的建议确实来自框架公开协议，而不只是模型旁白。随后重新填写 artifact 角色，是模型对 expected effect identity 的推断；validator 没有提供能合法建立这个新实例关联的可执行声明。

七次 `provider_feedback` 与下一 request 最新 runtime_observation **完整 JSON 相等**，并保留此前候选和反馈。证据没有支持字段反馈丢失、整体上下文丢失、Skill 重选。问题是反馈同时呈现“新资源不许这么绑定”和未实例化的 Skill identity；这些局部约束当前不能组合成合法写法。要求降低为单一错误码会进一步掩盖第二个 required-workspace 冲突。

## C 的表达性验证及两组矛盾

`candidate_C_rejected.json` 是紧凑完整尝试，**不是合法 C**。只读三输入；check_contig_compatibility 输出 observation，outputs=[]；配置文件为 immutable plan-local artifact；工作区为新 mutable directory；prepare establishes `database_layout_prepared`，build requires 该状态并 establishes `snpeff_database_ready`；dependencies 保留先后；annotation 读取相同 workspace，产生正式 VCF，随后 ANN summary 和原任务报告交付。所有原必需输出保持必需。

### 矛盾一：资源角色与实例身份没有公开连接

- Skill `workspace_mutations.workspace_ref=artifact.snpeff_data_dir` 是冻结能力 effect 的逻辑角色。Skill 的 generate binding subcontract 又把同一文本放在 `artifact_binding_requirements.snpeff_db.canonical_resource_id`，在执行绑定检查中比较精确相等；不是自动生成的本任务工作区身份。
- 新工作区由 `proposed_artifacts[*].local_ref=plan.*` 声明。省略 binding 时，`plan_bindings.py:369` 生成 `candidate:<scope>:<local_ref>`，aliases **只包含局部 ref 与该 identity**，initially_available=false；没有物理目录存在性要求。
- `_canonicalize_workspace_effects` 对显式 effect 和 capability effect 都只做现有 exact alias 查表。候选实例能解析；Skill 的 artifact 角色不能解析，原字符串保留。
- closed effect 检查因此要求候选实例属于仅包含 artifact 角色的集合，失败。显式改用 artifact 角色或省略 mutation 以继承 Skill，mismatch 消失，但变成两条未绑定 mutation，且 annotation 的候选工作区没有可达状态来源。
- 试图用 resource_binding 将二者关联，又被首道严格检查拒绝，因为 artifact 角色不是已声明 authority。改 local 名字不解决；把它放进 local_ref 也不能越过 plan.* 与 candidate scope 约束。没有公开字段能授权“新实例实现这个能力角色”。

这是**public Planner surface 与 admission 的接口缺口**，不是“必须审批后才分配 ID”的时间环。候选局部身份已经在 effect 检查前生成；缺的是角色关联。同一个 prepare/build 有序 mutable resource 的表达意图本身并没有被正确传到 closed effect identity 检查。

### 矛盾二：可变工作区同时被要求是不可变输出

`plan_contract.plan_result_contracts` 将每条 proposed_artifact 的 required 纳入结果表；`plan_tracker.validate_output_contracts` 只计 `artifact_usage.relation=produce` / step.outputs。因此 required workspace 必须加入 outputs 才满足覆盖。`step_effects.compile_plan_dataflow` 又明确要求 PRODUCE 目标是 immutable_artifact，mutable workspace 放入 outputs 会触发 `PLAN_MUTABLE_RESOURCE_DECLARED_AS_OUTPUT`。合法有序 MUTATE 不是这里的 producer。

这组前提在本 session 的 result_contract_version=1 与 required=true 下冲突。不能把工作区改可选、改成用户已有输入、合并/删 prepare/build、预建目录或改用公共数据库。去掉显式 result-version/required 扩展的公开资源例子变体仍存在第一组缺口，不是合法通过方案。

### 真实入口与身份时序

生产入口仍是原生 JSON display/envelope parser → `validate_planner_candidate_deterministically(validate_dataflow=False, require_declared_references=True)` → 资源编译/局部 allocation intent → `resolve_plan_proposal_handles` → strict `build_plan_state` → `normalize_plan_contracts` → `compile_plan_step_effects` → `compile_plan_dataflow` → Skill/能力校验 → derive validation → final 编译/状态保存。没有替换 validator 或直接返回 admission=true。

正式分配在 `plan_tracker._prepare_atomic_plan_objects` 的锁内进行：promote_allocation_intent → 改写局部引用、usage、资源别名 → 再编译 → 冻结 Plan/待审批对象。磁盘目录是执行阶段的工作，不是此时必须预存。B 在正式分配前拒绝，只保留诊断对象，无执行权；7 次合成拒收前后 steps/plan_artifacts/current_approval 都为空。新任务重复测试与路径/局部名变化结果一致。

全图使用生产 Consultant/Planner/typed phase/routing，只有 provider 和宿主发现是固定替身。为避免 Consultant 自带输入扫描触及正式文件，全图三输入和输出根放在新的不存在的中文空格路径；没有用路径改变制造通过。另以**正式路径原字面声明**走真实 parser/context/resource/effect/dataflow/结果覆盖接口，仍重现同样缺口，见 `formal_path_text_native_seams`。后者不冒称完整全图，也不替代前者。所有源字节与 Skill 的加载 hash 单独登记。

## 预算、重复、状态隔离与取消

本 session 实际 budgets 的两项都是 4；不能套用其他 session。生效计数 `planning_epoch.planner_calls_total=8`，candidate_repairs=0。初次调用计入；每轮源 Planner 的调用 claim 在 dispatch 前通过真实 CAS 保存。这里的纠正由普通 Planner turn 承担，没有扣旧 candidate_repairs 通道。不同 candidate scope 不重开本 session epoch；实际请求保留同一 phase prefix。

- 外层 `bio_agents.py:38437` 不是单独的硬 4 次上限，而是 calls>=4 **AND** repeat_count>=2。
- 更早的 `planner_recovery.attach_rejection_facts` 会在连续相同拒收 repeat_count>=1 时生成 `PLANNER_REPEATED_FAILURE`。A/A 正例实际只调用 2 次，并自主终止；变 title/public_message 与新 UUID 不影响这个结构未编译候选的判定。
- A/B 交替只与最近一次 failure 比较，错误目标切换令 repeat_count 一直为 0。因此 7 轮没有自主收敛。
- 另检查 compiled B 的 UUID 对结构签名的影响：issue fingerprint 会规整 `candidate:<scope>:plan.*`，但 `_candidate_correction_signature` 使用编译后的 step_effect identity，未同样规整候选 scope。重复相同 raw B 可被误算为结构变化。详细签名/重复计数见 `repeated_B` witness；这是不同于 A/B 相邻比较的第二个收敛问题。
- phase 还有 max_turns=32/max_epochs=4 的独立边界，见 `bio_agents.py:3330`。本轮不跑到这个边界，不声称系统无限循环或已经在 7 轮自主终止；原事故由用户取消，合成 A/B 用明确外部上限在第 7 个落盘拒收后停止。
- 同 claim、同 raw_digest 的 result 重投由 `complete_planner_claim` 幂等处理；unsettled claim 不能重新 claim。新建合成状态的真实 save/load、独立进程再 load 均保持计数 1。本轮没有把原 session 送给 loader。不同调用的相同 raw 仍是新事件，不能按内容 hash 退还调用。
- LIFE 保护：真实只读 Plan 达到待审批后，将错误新候选交给现有拒收 producer；旧 steps/旧 submission 保留，但两个真实 review readiness 都为 false。没有借旧 Plan 送审。

**取消另有已复现缺口。** 原事故第 8 调用回执 role=cancelled，task/runtime 已落盘 cancelled，state generation 58→59；原事故无第 8 候选，没有后续 dispatch/接纳记录。新合成注入证明：provider 等待期间 `_commit_user_cancellation` 落盘后再返回完整响应，当前 source 仍跑 validator；若响应本身合法，steps 与 pending approval 对象被保存，task 仍 cancelled。TypedPlanner 最终投影 terminal，不继续路由审批/Generate，但“取消后不接纳”未满足。

相关边界：node_context 的语义过期检查不包含取消状态，忽略单独 state_generation 变化；planner claim 的 `_check_scope` 只核 task/revision/epoch；最终保存接纳也没有针对已取消任务的不可提交条件。应在现有生命周期的 dispatch、post-await 和 atomic admission 加取消前提，不能把所有非语义 generation 变化一概改成过期。需要修复的是 late admission，不能从原事故推断实际发生过这个 late-valid 分支。

provider 的 CLEANUP_CONFIRMED 是用户终端事实；本 session 独立 task save/cancel receipt 已确认。工具/全部子进程/OS 当前状态及整体 cleanup 未有完整回执，保留 UNKNOWN。metrics commands=0、科学 attempts=0 不等于 Consultant 从未做只读检查；安全记录确有 input_inventory/runtime_evidence 预处理，未全盘检查或探测进程。

Consultant 重复正文已能区分：`0004` 与 `0005` 是两个不同 call/message ID，public_message hash 一样。前者收到 `TASKSPEC_MUTATION_REJECTED / PLANNER_FEEDBACK_PROPOSAL_INVALID: supporting_evidence is required`；后者收到 accepted selection observation，再由 `0006` explicit submit_planning_handoff 提交。不是仅凭文本诊断双订阅，也不是 Planner 中重新选择 Skill。

## 回归归因与既有修复

| 范围 | 隔离证据 | 分类 |
|---|---|---|
| 上一轮 S1–S3 前后 | `e09b39f8df0afdd87321464e17e7e2c8f3b2c67d` tree `924577fae3aec082eb65199eab8d6478a789a980` 与当前 HEAD；各自整套源码/prompt/Skill导出；同一 C 都报上述5条；同一真实只读能力都到待审批 | **PREEXISTING_GAP**，最早动态核验范围限 e09b39f8 |
| 当前 bare tree 与当前 worktree overlay | C 及只读正例结论一致，生产文件与加载 hash 登记 | 无证据表明五份 overlay 引入本次两个表达缺口 |
| UUID伪进展、取消后late-valid接纳 | 在完整 e09b39f8 导出上另跑同B重复与取消后合法只读候选，两项均重现当前缺陷 | **PREEXISTING_GAP**，核验范围同样不早于e09b39f8 |
| 更早资源协议修改 | `c5fe444dd968c9c6004eb5b0503276cdacac43ff` 将原 explicit role-alias 描述改为 existing-resource strict reference，并增加新资源 resource_kind；源码触点明确 | **UNKNOWN** 最初回归归属：未取得其前版同科学语义、同整链合法 C 的接受证明；不把旧权限漏检当作应恢复的合法行为 |
| 本 session 加载混用 | 八个核心 live callable 与捕获源码匹配；当前 Skill snapshot一致；bio_agents loaded revision缺失 | 未确认 INTEGRATION_OR_SNAPSHOT_MISMATCH；完整加载 revision 仍 UNKNOWN |

不能把旧审计 hash 当当前部署，也不能把仅通过只读能力的历史 snpEff T18 当作 mutable prepare/build 验收。

上一份 S1–S3 **不只是规格文件**：当前 HEAD 的 `docs/implementation/cross_layer_20260914/README.md` 及历史提交确认已有实际实施回执；S1 `718adbcf…`、S2 `ff7c2b4e…` / `48f1fbe5…` / `68bf7453…`、S3 `5d327831…` / `a500d2ca…` 均在当前祖先链中。本轮没有接续执行或撤销它们。公开事实发布、Generate 修正账本、报告 journal、PF/LIFE/INPUT、CLI 与审批代码全部保持；本轮针对性的 LIFE witness 通过不等于重验了它们全部科学/产品验收。

## 最小修复设计（尚未实施）

1. **在现有资源声明中补显式、局部的角色关联**：`plan_contract` 公共 schema、`plan_bindings.compile_plan_resource_bindings`、`step_effects` 和同源 requirements/prompt。新工作区身份仍由系统按候选分配；仅允许把冻结 capability 中明确声明的 workspace role 关联到本候选已声明 local_ref。关联必须带 capability/step 范围，不能依据名字/目录/语义自动猜配，不能把 role 加入全局可访问资源表。具体新字段名称只是后续设计，不声称当前已支持。现有 resource_binding 继续只表示复用正式资源。
2. **同一关联贯穿已有冻结与执行绑定**：在现有 allocation rewrite/PlanSnapshot中保存和校验同一 identity，覆盖 final compile；`plan_bindings.py` 的 artifact-backed binding requirements（精确比对 canonical_resource_id）也必须从该已批准关联实例化。只放宽 StepEffect mismatch 会把故障推迟到 `snpeff_db` runtime binding，不能验收。未知/旧scope/跨candidate/未批准实例继续拒绝。
3. **结果覆盖按资源生命周期检查**：`plan_result_contracts` / `validate_output_contracts` 对 required mutable workspace 使用已验证的有序 mutation/state 来源，文件交付继续使用唯一 PRODUCE。禁止靠 required=false 或把 workspace 放进 outputs 规避；同工作区 prepare/build 的依赖、状态/evidence、权限均保留。不要另造一份 TaskSpec/平行合同。
4. **关联反馈与本地有界收敛**：同源反馈同时解释声明、角色关联、mutation refs、required-workspace状态，返回具体 field_path/actual/expected 与可选的真实声明，依赖上游无效的检查标记 not_evaluated。对现有 `_candidate_correction_signature` 规整系统分配的 candidate identity；在现有 Planner 生命周期内保留已见结构+错误目标组合，识别 A/B/A。使用持久 policy/claim 总额与现有 internal failure 出口；不增加预算、不恢复 Controller、不新增纠错 Agent。
5. **取消拒收 late candidate**：沿现有 cancellation receipt/任务终态，在 dispatch 前、await 后、锁内接纳前检查。late result 可记录 discarded receipt，但不能落成 pending Plan、审批对象或 executable capability。允许诊断落盘，保持任务/Plan语义变更已有 CAS 规则。

内部协议缺口应使用当前 `planning_failure → terminalize_planning_failure → TypedPlanner terminal/harness_error` 的内部停止事实，明确科学分析未启动。不能转成让用户填写 resource_binding 的生物学问询，也不冒称缺输入/数据坏/DB构建失败。

实施风险与保护点：角色关联若缺少局部范围会串绑两个工作区；冻结后若保留角色字符串会在 Generate 再次失败；结果覆盖若只检查“出现过 mutation”会漏掉状态与顺序；签名规整若抹掉用户声明或真实 effect 变化会误判有效修正；取消检查若把所有 generation 变化都当语义过期，会破坏既有候选生命周期。分别以上述范围约束、重载后的绑定消费、缺状态/乱序反例、真实字段变化正例和非语义 generation 正例限制补丁范围。

### 必须组合通过的后续验收

同一公开 schema + 同一真实 Skill + 同一原生全链：新 workspace 不存在 → readonly observation → prepare → build → annotation/ANN正式必需输出，应到待审批；通过后再次冻结/重载/approved-binding消费仍保持同一资源和正确状态。删除任一关联/顺序/evidence、改成未知/过期/跨scope引用、缺任一 required final output 均应拒绝。两个独立本地 workspace 使用相同能力角色也不能串绑。C 成功只能算 Plan FIX，不代表执行/科学 FIX。

关联测试还必须同时满足：A→B→C 全字段反馈实际可达且最终接纳；A/B/A 与同B新UUID有界自主停止；同事件幂等/新进程重载不退额度；旧 Plan 不借资格；取消前/中/后与晚到合法候选均无新dispatch/Plan/approval/执行；保持 CLI/输入/审批/候选生命周期既有保护。需共享 mutable Skill 交叉用例：当前检索到非空 workspace_mutations 的真实 Skill 只有 snpEff；prepare/build 两个真实调用方覆盖共享路径，其他 Skill 的只读/独立文件对照可用，但不能伪称发现了第二个 mutable Skill。

## 本轮测试与材料

`verification.json` 保存每个测试名/结果、before/after/current 的生产 SHA-256、各实际反馈和状态摘要；`admission_replay.json` 保存新增隔离真实准入实参；`evidence.json` 是原事故静态证据。脚本为 `project_evidence.py`、`collect_verification.py` 和 `tests/export_plan_resource_cycle_audit.py`。新增测试只有 `tests/test_plan_resource_cycle_audit.py`。

| 任务矩阵 | 本轮结果/边界 |
|---|---|
| P01 | 原始7候选正式字段经生产全链重现；原路径另做真实接口检查；完整错误/反馈证据齐全，历史逐函数内存未录制 |
| P02/P03 | C拒绝及两组具体冲突 witness PASS；**合法C/FIX PASS不存在**；无预存工作区，无合并/删除 prepare/build |
| P04 | 现有真实资源编译器正/负例11项通过，覆盖当前引用、未知capability域、过期revision/scope/alias、mutable状态顺序；不是端到端既有workspace执行验收 |
| P05 | readonly真实能力到pending；C的workspace/final结果角色冲突已定位 |
| P06 | 原7轮反馈与下一请求完整相等；新图捕获接线；因C无法准入，“固定纠正C通过”是待修验收，不虚报通过 |
| P07 | A/B/A有界7轮、相同A改标题自主2轮、相同B新UUID的签名见证；外部上限与自主终止分开 |
| P08 | 真实CAS claim、重复result幂等、持久重载和独立进程计数保持；不重置epoch |
| P09 | 拒收正式状态隔离；真实旧Plan/submission保留而review资格关闭 |
| P10 | 同真实Skill的prepare/build共享mutable能力与真实只读对照；未发现第二个非空mutable Skill |
| P11 | 不存在的中文空格路径、不同局部名称/说明均覆盖；不依赖科学内容或隐藏答案 |
| P12 | 原取消仅静态；新图取消后晚到合法/非法响应验证了late admission缺口；整体OS cleanup UNKNOWN |
| P13 | e09b39f8 vs 当前bare、当前overlay，C与readonly同结果；PREEXISTING_GAP，不声称最早回归已确认 |

运行环境是 allowlist `/tmp` 导出、指定 Python、`env -i`、合成配置和禁止网络/外部进程/正式路径读的 audit hook。全图第一次接入正式路径时，guard 拒绝了 Consultant 的读尝试，未读取文件内容；随后才改用合成路径，并增加正式路径纯接口测试。测试实现调试中的临时目录/CAS与不支持字段错误均已在新增测试中纠正；没有改旧断言或放开保护。

复现依赖当前工作区已有的隔离 harness：其中 `tests/test_plan_local_preaction_convergence_v2.py` 开工已有修改，`tests/test_step_effect_runtime.py` 和探索性旧 `tests/test_planner_agent_loop.py` 开工未跟踪。本轮不收编这些文件；导出 manifest 登记实际加载字节。before/after 使用同一外部测试 fixture，生产源码/prompt/Skill 各自来自对应完整树。本提交不是可脱离这些已有 harness 文件的独立发行包；在干净克隆复跑前需按 manifest 准备相同安全测试依赖，不能把缺失依赖误判成生产行为变化。

最终重点组：新增审计测试及既有资源协议测试 **22 passed, 4 subtests passed**（新增审计15项；既有资源协议7项及4子项），已经包含 UUID与标题进展的两个测试；此前这两项的独立运行也是 **2 passed**，不额外累加。独立进程重载 **1 passed**；before **4 passed**、after **2 passed**。见 JSON 原始逐项回执与下方命令，以上命令rc均为0。所有 PASS 的含义以各 witness 断言为准；JSON 的 call_outcomes 包含子测试回执，不将它们误报为26个独立测试函数。

另外运行了原有未跟踪 `tests/test_planner_agent_loop.py`：**10 passed / 9 failed**，9项都在旧 fixture 调用 record_plan_review_submission 时缺当前 candidate scope，报 `PLAN_REVIEW_CANDIDATE_SCOPE_STALE`。原文件字节未改，失败完整保留在 verification；不把该探索性旧测试组算全绿，也不为迁就旧fixture削弱LIFE。新增真实接纳→拒收→review readiness用例通过。

可复用执行方式（只写新 `/tmp` 路径；命令中的 RUNROOT 为示意，复跑需选未存在目录）：

```bash
python tests/export_plan_resource_cycle_audit.py /tmp/NEW_RESOURCE_AUDIT
mkdir -p /tmp/NEW_RESOURCE_RUNTIME /tmp/NEW_RESOURCE_RESULTS
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 TMPDIR=/tmp/NEW_RESOURCE_RUNTIME \
  BIOMIND_RESOURCE_AUDIT_RESULTS=/tmp/NEW_RESOURCE_RESULTS \
  BIOMIND_OFFLINE_RESULTS=/tmp/NEW_RESOURCE_RESULTS/pytest.json \
  /public/home/liusiqi/anaconda3/envs/biomind/bin/python \
  /tmp/NEW_RESOURCE_AUDIT/tests/offline_managed_bootstrap.py /tmp/NEW_RESOURCE_AUDIT \
  -q tests/test_plan_resource_cycle_audit.py tests/test_planner_resource_protocol_v2.py
```

原事故提取器只静态读指定 session；不要将它当业务恢复入口。Git 提交仅包含本目录与上述两个新增 tests 文件；最终 commit hash/完整文件列表由交付消息和 Git commit 自身给出，避免文件内自引用 hash。开工442项中，437项状态仍在，5个原未跟踪的 remote-workbench 路径由并行任务提交；本轮不把它们描述成仍未跟踪。期间另观察到14个该项目新增路径，也由并行任务提交；本轮未读取、修改或暂存这些路径。源码/索引保护核验见 `preservation.json`；**未 push**。

Git whitespace 检查：全范围 rc=2，仅指向原样 overlay patch 的空白上下文行与 TSV 取消行的空列分隔符；保留证据字节。排除这两个原始证据载体后的代码/报告检查 rc=0，详见 preservation.json。
