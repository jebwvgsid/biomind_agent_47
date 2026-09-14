# Plan 资源协议、主动结束与有界收敛：增量实施回执

本轮实施依据为用户最新的 V1 生产修复请求。前一份仅审计任务书不继续限制本轮已授权的生产改动，也不触发重做上一轮 S1—S3。原事故不恢复、不读取。本轮正向结论来自新合成任务的真实生产准入与路由，历史审计 witness 仅作缺陷依据。

## 结论与版本

- **Plan 表达/绑定 FIX PASS**：C_FIX 仅为历史 C 的新工作区声明增加三项公开角色关联，六步科学语义、三项正式输入和全部交付义务保持；全准入链到 pending approval。审批后冻结、重载、Generate 实际请求及 artifact-backed binding 使用同一正式实例。
- **收敛与主动停止 FIX PASS**：四次总额包括首轮；第四次合法可接纳，第四次非法终止，无第五次。A/B/A 在第三次停止；仅系统 candidate UUID 不同在第二次停止；模型正式主动停止可在剩余二次时终止，后续 dispatch 为零。
- **取消准入 FIX PASS**：取消先成立时 late-valid/invalid/STOP 不创建新的 Plan、approval 或执行资格；Plan 先接纳则保留历史，取消关闭后续资格；非语义 generation 正例正常接纳。
- 固定 provider 与显式科学 stub 只证明接口与状态机。**真实模型质量、SDK 对外网络行为、真实 snpEff/Java/科学端到端和 OS 子进程清理未验证。** planned state 不等于科学成功。

BASE/审计锚点：`f78bb10bb3f12059efaf911916940440cecdd73d`；实际起点 HEAD 与该值相同，未 checkout。最终生产源码 HEAD：`9fc2081adbeedf93ba197209583dd807ea99551f`。后续材料提交只收本轮测试支持、等价 fixture 与回执；其完整最终 hash 由提交回执返回。分支：`refactor/consultant-controller-authority`。

最终实际加载：`acceptance-current` 安全导出，237 个允许的源码/Skill/fixture 文件，签名 `fcd6a54c665deebabf7d6452bf2161c71b7c4f2788686f88aed8bc260feaa659`。逐文件、历史合成 fixture、五个保护 overlay 的 committed/loaded SHA-256 见 `final_export_manifest.json`；真实 import 路径、日志及运行命令另存。测试对象是**本轮提交源码 + 原有五个 overlay + manifest 指明的安全 fixture**，不是历史树，也不宣称裸 commit 独立部署已验收。

## 字段修复与公开表达

已证实的两组 PREEXISTING_GAP 保持旧审计范围：角色 `artifact.snpeff_data_dir` 被当作本次正式资源引用，但新实例只由声明获得 candidate ID；required mutable workspace 又同时被要求 immutable PRODUCE、并被禁止放入 immutable outputs。本轮不追溯最早引入提交，不以时间先后判回归，最早起源仍 UNKNOWN。

历史 A 的 `resource_binding.resource_ref=artifact.snpeff_data_dir` 无正式声明，第一道报 `PLAN_RESOURCE_REFERENCE_NOT_DECLARED`。历史 B 删除它后，两条 `STEP_EFFECT_PROTOCOL_INVALID` 分别位于 prepare 的 `$.steps[1].step_effect` 和 build 的 `$.steps[2].step_effect`，内部为 `STEP_EFFECT_CAPABILITY_MISMATCH`；role 与 candidate 实例被合并成不同 mutation，另产生两个 binding missing，再加 required workspace 覆盖缺口。原 B 完整五错误不能缩写为终端显示的三个。旧审计完整链和失败账本保持原文件；本轮新增链见 `chain_index.json` 与 `chains.json.gz`。

公开新增字段在原 `proposed_artifacts` 声明中，完整可用候选见 `candidate_C_FIX.json`：

```json
"role_associations": [
  {"step_id":"prepare_database","capability_id":"genomics.snpeff_annotation.prepare_database","role":"artifact.snpeff_data_dir"},
  {"step_id":"build_database","capability_id":"genomics.snpeff_annotation.build_database","role":"artifact.snpeff_data_dir"},
  {"step_id":"annotate_variants","capability_id":"genomics.snpeff_annotation.annotate_variants","role":"artifact.snpeff_data_dir"}
]
```

这是 `plan.snpeff_data_dir` 的关联列表，每项严格限定本候选 step、已选 capability 和真实 Skill role。同名角色可在不同步骤组指向不同实例。没有全局 role→instance 字典、名字配对或第二份可编辑合同。`resource_binding` 继续仅复用当前正式资源；新资源省略该字段。模型只填 local_ref；candidate scope 与正式 ID 均由系统分配。

编译先验证关联，再按步实例化 expected effect 和显式 effect；错误前提未满足时依赖检查标 `not_evaluated`。资源关联随 promotion/ref rewrite 写入 PlanSnapshot，冻结 operation 的 artifact binding requirement 引用同一正式资源。消费端从批准 snapshot 投影，核对 task/Plan/step/capability/identity/revision，未批准或过期/外来身份继续拒绝。prepare/build 为 read_write；annotation 仅 read_only。原正式资源复用分支另有回归，防止 canonical row 合并时丢关联；它不用于伪造 C_FIX 的新工作区正例。

required mutable coverage 使用已封存、核对一致的 dataflow receipt 及 MUTATE 的状态/evidence来源，消费者前提仍需满足：新目录不存在 → prepare `database_layout_prepared` → build `snpeff_database_ready` → annotation 读取同一实例。immutable 必需文件继续完整 PRODUCE；缺 prepare/顺序/状态/evidence/最终文件均拒收。Skill 唯一修改为 annotation artifact requirement 增加 `required_state: snpeff_database_ready`。新关联 snapshot 最低 reader 1.2.2，旧合法快照保留 1.2.1 语义和资格，不静默补关联。

Generate 正例使用真实 TypedGenerate、冻结定义、public binding normalization、operation/workspace gate。批准前不能读取批准 snapshot；批准后目录仍不存在。测试只对科学前置 observation 和输入设置显式合成 fixture，返回的代码未执行。实际请求与 `action.env.SNPEFF_DB` 和 `formal_workspace_bindings` 指向批准路径。双工作区测试为合成扩展，中文/空格路径与重命名不串绑；不虚报第二个真实 mutable Skill。

## 完整反馈、四次总额、主动停止

每轮归档原始模型响应、实际 validator args/kwargs/result、持久完整失败、下一次实际 provider 消息，按 ordinal 相连。反馈来自生产 validator/账本；固定模型的下一候选只作为替身输出，不能把模型诊断升级为框架事实。新缺关联错误通过现有 `step_key` 向 provider 给出准确 step，消息明确它应填写到 `role_associations[].step_id`，并保留 capability、role、来源、字段路径和 `not_evaluated`。原 overlay 过滤规则不变。

CAS claim 是总额度入口，最多 4 次且包括首轮；Skill context 回复不退额；正常回复、协议纠正均占用额度。稳定签名只在比较视图将当前候选系统 ID 反查 local_ref；保留能力/Skill、关联、顺序、状态/evidence、参数和真实用户字符串。epoch 保存有界已见结构/错误目标/可信事实版本，识别 A/B/A；合法第四次回复不被重复检测否决。已结算 claim 重投幂等，未结算不重复派发，终态重载不续额、不自动换 epoch。

Planner 单独使用 `max_retries=0, cache=False` 客户端，应用层每 claim 只有一个 transport attempt；修正客户端绑定同一策略。真实 node→invocation registry→run_provider_attempt→固定底层 wire 的测试证明第四次到达四次 wire、每条记录 `sdk_max_retries=0, attempt_ordinal=1`；传输失败只调用一次。Generate 和 Recovery 的既有策略保持。

公开停止是同一 JSON envelope 的正式互斥分支：

```json
{
  "outcome_type": "STOP_PLANNING",
  "diagnostic": {
    "stage": "plan_admission",
    "error_codes": ["PLAN_RESOURCE_ROLE_ASSOCIATION_MISSING"],
    "attempted_changes": ["Removed resource_binding from the new workspace declaration; an explicit step/capability role association is still missing."],
    "stop_reason": "I cannot determine a supported untried correction from the available declarations and feedback.",
    "uncertainties": ["The missing role association has not been resolved; this does not establish a scientific or data failure."],
    "unperformed_work": ["prepare_database", "build_database", "annotate_variants", "ANN summary and formal report"]
  }
}
```

示例由 `planner_stop_wire_example()` 同源进入实际请求，无占位值。实际 prompt 明确额度未耗尽也可在没有可支持的新修正时主动结束。STOP 不附空 Plan，不能混合 Plan/Skill/reconciliation 字段；仅通过完整 envelope、scope/staleness 和字段校验后进入既有 `planning_failure → terminalize_planning_failure → TypedPlanner terminal`。沿用现有单个完整 JSON fence 的 transport 等价规则，文本/不完整 code fence、截断、关键词 `<finish>` 不路由。`model_diagnostic.source=model_report, verified=false`，真实错误另存。停止不代表任务科学完成，也不宣称报告已写出。

| 新合成情形 | Planner 已占用/实际响应请求 | 结果 | 后续 Planner/Generate dispatch |
|---|---:|---|---:|
| C_FIX | 1/1 | pending approval | 未越过人工审批 |
| A/B/A | 3/3 | PLANNER_REPEATED_FAILURE | 0/0 |
| 同 B，仅系统 UUID/title 改变 | 2/2 | PLANNER_REPEATED_FAILURE | 0/0 |
| A/B/evidence/C_FIX | 4/4 | 第四次 pending approval | 无第五次 |
| 四种不同失败 | 4/4 | PLANNING_EPOCH_EXHAUSTED | 0/0 |
| B/正式模型 STOP | 2/2 | PLANNER_MODEL_STOPPED，remaining=2 | 0/0 |
| 首次正式模型 STOP | 1/1 | PLANNER_MODEL_STOPPED | 0/0 |

负例测试的外部上限只用于单次拒收观察，账本明确 `external_stop`；上述自主停止测试全部 `external_stop=[]`，提供额外未消费候选并恢复 graph 以证明没有后续 dispatch。独立 reader 进程加载真实四次终态 store；计数/epoch/已见 ledger 未变，新 claim 被拒，completion 重放两次无写入。过程独立性以分开的真实 subprocess 命令和 process nonce 记录，避免 PID namespace 重号误判。

## 取消与既有保护

复用任务 status、当前 revision cancellation receipt、terminal planning_failure；检查入口/claim 新读、claim CAS 锁、派发前、底层 provider 前、post-await 和 allocation/Plan/approval 发布前的 atomic lock。取消优先于 CAS 重派或晚到 STOP；只保存 discarded-response 诊断。

| 确定屏障位置 | 实际请求/占用 claim | 新 Plan/approval 对象写入 |
|---|---:|---:|
| claim 前 | 0/0 | 0 |
| claim 锁前取消先提交 | 0/0 | 0 |
| claim 后派发前 | 0/1 | 0 |
| provider invoke 前 | 0/1 | 0 |
| await 内取消，晚到 valid/invalid/STOP | 1/1 | 0 |
| validators 全过后、atomic admission 前 | 1/1 | 0 |

屏障调用真实取消事务，不靠 sleep。Plan 先落盘再取消保留 snapshot/审批历史，真实 TypedGenerate 请求数为零。非语义 generation 增量保持 C_FIX 接纳。没有将整个 generation 变化一律作为语义过期。

保留原有 publisher（S1）、Generate 账本（S2）、report journal（S3）、PF/LIFE/INPUT/CLI/审批；相关测试与跨层图组合运行。未恢复 Controller，未加全局类型配对/语义 binder，未以假结果、数据损坏或数据库失败掩盖内部协议问题。

## 验收、失败账本与复现

| 最终实际运行 | rc | pytest 结果 |
|---|---:|---|
| acceptance-current-combined | 0 | 184 passed, 3 warnings, 4 subtests passed in 329.07s (0:05:29) |
| acceptance-current-cli | 0 | 15 passed in 1.83s |
| acceptance-current-reload | 0 | 1 passed in 0.45s |
| acceptance-current-epoch | 0 | 1 passed in 5.29s |
| acceptance-current-legacy | 1 | 9 failed, 10 passed in 5.25s |

combined 含 C_FIX、11 项资源反例、冻结/Generate、双工作区、主动停止/预算/SDK、取消、独立重载 writer/reader、九项合法 scope 等价回归以及 S1—S3/PF/LIFE/INPUT/审批/CLI progress。CLI display 全模块在独立进程运行；frozen 与 exhausted epoch reader 再分别以独立进程重载。表中 subtests 单列，不冒充额外普通测试。原九个 legacy 失败节点与旧账本逐项相等。

完整命令含 cwd、替换环境、argv、rc、时长及结果路径见 `final_commands.json`。安全 exporter 仅复制允许文件；bootstrap 禁止网络、非允许 subprocess/正式路径读取，注入公开的合成 config（空凭据），不用进程敏感环境。所有正式 schema/parser/registry/resource/effect/dataflow/result gate、candidate scope/claim、StateContractStore、审批资格、typed routing 和 runtime binding 均为生产实现；替身边界列在各测试与链中。

复现顺序：导出当前修复与五个原 overlay；创建新的 `/tmp` runtime、results、artifacts；按 final_commands.json 的 combined、CLI、frozen reload、epoch reload 顺序启动独立 guarded Python。最后单独运行旧 fixture 控制并预期记录其九个失败。`tests/test_plan_lifecycle_scope_fix.py` 用生产 graph 的真实 scope 新增九个等价业务回归，不填造 UUID、不放宽 LIFE、不 skip/xfail；原 `test_planner_agent_loop.py` 未修改或整体收编。

旧九失败账本保留在 `preserved_nine_fixture_failures.json`，旧审计原 ledger SHA-256 为 `19b2de5ea0f7cba57ebafbf789e30b1723b781778a0298e69296ce18a57ed8d7`。新完整失败与开发运行台账见 `verification_ledger.json`。不得将旧 witness PASS 等同本轮 FIX PASS，也不宣称全仓库全量全绿。

另一个已定位测试顺序问题：`test_output_router_sources_and_identity_do_not_guess_authorship` 在父 ContextVar 留下确认 ticket，后续 `test_context_local_confirmation_has_no_cross_call_wait` 继承它。修复前安全 baseline 和修复后相同两用例顺序均为 1 passed/1 failed，三个相交 CLI 文件字节未变；CLI 模块独立为 15 passed。未改动 CLI 生产或原 fixture；最终将完整 CLI 模块在同一源码的单独进程运行，单进程组合失败也保留。首次最终组合还因运行命令未创建 artifacts 回执目录而失败；后续运行明确 mkdir，不修改任何生产准入断言。

## 提交与原现场

完整逻辑提交与逐文件范围见 `commits.json`。每片在对应测试通过后用 alternate index 从当时 HEAD 隔离本轮 delta，校验原 index/HEAD 未并行改变，再 CAS 更新当前分支；只同步本轮条目进真实 index。五个原 overlay 未整体纳入提交；一处相交反馈改为使用已有 machine_evidence/step_key 后安全提交。失败的隔离试探未更新分支或真实 index。

`protection.json` 校验原 437 条 staged/dirty/untracked 状态逐项不变、原 index 在本轮拥有路径外无变更、staged diff 仍为空；五个 overlay 重新应用到本轮 HEAD 后与当前 worktree 精确相等。旧审计报告未覆盖。全部逻辑提交都延续当前分支，没有 checkout/reset/stash/clean、恢复原 session、读取启动脚本/密钥/敏感环境或正式科学数据；没有安装、真实 API/科学调用和 push。
