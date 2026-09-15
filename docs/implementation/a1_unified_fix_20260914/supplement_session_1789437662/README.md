# session_1789437662：真实 Plan 与 F6 差异补充验收

**复核完成；生产未修改。第三候选存在一个陈旧步骤引用。只改这一字段，保留其余完整候选及原任务义务，当前正式准入可以通过。现场真实模型没有作出此修正，而是在第四次响应中正式主动停止。** 本结果不是原科学任务成功，也不是新生产 FIX PASS。

| 回执字段 | 结论 |
| --- | --- |
| LIVE_STOP_REASON | 第四原始响应为 STOP_PLANNING，经正式 parser/claim 接纳为 PLANNER_MODEL_STOPPED。共四次调用，repeat_count=0；第三候选拒收与第四响应分别有证据。没有获批 Plan、科学 attempt 或 Commit。 |
| EXACT_ADMISSION_PREDICATE | association.step_id=prepare_database，但第三候选已无此步骤。steps.get(...) 返回空对象，首个 OR 条件 `None != genomics.snpeff_annotation.prepare_database` 为真并短路；不是角色 access 或父资源类型错误。 |
| TEST_LIVE_DIFFERENCE | 旧 F6 人工候选把 step_id 与 operation 设为同值，同步预填依赖、角色、config 和读输入；真实候选把 prepare 能力移到 runtime_healthcheck，角色引用没同步。两边输出声明/结果模式也不同，不能直接互换。 |
| ORIGINAL_FIX_STATUS | 原 F1—F6 已记录的协议/helper/进展/AskUser/审批输入与固定 Plan 下游机制结论保持原范围；真实 Planner 从公开请求形成/纠正合法 Plan 的可靠性尚未证明。 |
| MINIMAL_FOLLOWUP | 原主单追加现有诊断、公开反馈字段投影、Skill ID 示例及 F6 变体接线验收；具体文件与正反例见原 changes_and_acceptance.tsv 新增行。生产状态均 NOT_IMPLEMENTED。 |
| REAL_MODEL_VALIDATION | 本轮新调用 NOT_RUN。仅静态检查这一次真实历史失败；没有估计成功率。 |
| SCIENTIFIC_E2E | NOT_RUN。新回放止于 approval_pending 或正式停止，零 Generate/Execute/Commit。 |
| PRODUCTION_CHANGED | NO；原 session NOT_RESUMED；旧结果未覆盖；PUSH NOT_PERFORMED。 |

实际 BASE 为 `1d0ab2cc6bd8d0c9e7ee2990725f8d41cd92c9c2`，tree `942d46cb728a56aee4344e5649d2fa2d06a1ff4e`，分支 `refactor/consultant-controller-authority`，带原保护 overlay。审计提交父版本和测试字节分别登记于 [baseline/start.json](baseline/start.json)、[最终安全导出 manifest](final_manifest.json) 与 [验证账本](verification.json)。没有重启全仓审计或重跑无关旧结果。

## 原始链与首偏差

| 原始调用 | 真实偏差 | 下一实际请求 / 后续行为 |
| --- | --- | --- |
| 0006 / P1 | annotate_variants 已有 workspace role association，却在 inputs 缺 plan.snpeff_data_dir；READ_MISSING 合理拒收。 | 前候选和反馈确实进入 0007；P2 只补这个读输入（另改说明文字）。不能写成第一次缺 association。 |
| 0007 / P2 | 两个独立检查 execute 步骤无 capability；prepare 未输出其必须产生的 config。完整反馈共五项，另外两项是 STEP_EFFECT_UNSPECIFIED。 | 五项进入 0008。P3 合并检查到受支持 contig/prepare 步骤，补 config 声明与生产消费。 |
| 0008 / P3 | 八步改六步，prepare_database 已删除，其 capability 在 runtime_healthcheck；workspace association 仍引用旧步骤。 | 0009 收到完整 P3 和错误 message；结构化 actual.step_id 却在公开投影中被删除，requirements.item.step_id 也被删除。 |
| 0009 / STOP | 正式 STOP_PLANNING；“校验器自相矛盾”是模型解释，并非已证事实。 | 无第五次请求。当前回复机会剩 1 与回复后剩 0 指不同计数时点；规则仍允许合法第四候选。没有证据说明代码提前强迫停止。 |

[四次原始业务输出及请求链](incident/findings.md)、[逐轮配对](incident/four_call_chain.json)、[完整候选变化](incident/candidate_deltas.json)、[反馈投影损失](incident/feedback_projection_deltas.json)保留可复核材料。响应文件只含已记录公开业务字符串，不含私有 reasoning；request.safe 是应用消息快照，不冒称网络 wire 原包。首轮与末轮均无 length 截断，四份 manifest 均未触发 compaction；不归因为上下文丢失。

## 精确谓词与最小修正版

`agent_core/plan_bindings.py:473–478` 使用候选自己的 step_id 建表，再按 association.step_id 查表。P3 第一条关联的父资源为 `plan.snpeff_data_dir` / `mutable_workspace`，角色为 `artifact.snpeff_data_dir`；正式 capability 的对应角色允许 mutate 且 resource_kind 相同。失败发生在任何身份归一化之前，未观察到编译器只转换一侧引用。

查表为空后，首条件 capability 不相等立即短路。对同一实际入参单独计算，Skill 成员检查也不满足；这是反事实逐项真值，不是该次执行了第二个判断。角色存在、父类型匹配。完整局部入参、首返回及真假值见 [exact_predicate.json](exact_predicate.json)。当前最早返回 expected.capability_id 为 null，持久诊断清理掉它；历史最早原异常对象未保存，不能用当前回放补造历史 null。

P_min 仅有这一个结构差分：

```text
$.proposed_artifacts[0].role_associations[0].step_id
prepare_database → runtime_healthcheck
```

[P_min.json](P_min.json)保留 P3 的全部 steps、success_criteria、metadata、required、source_refs 和五项交付 materializations。所有用户约束由新正式 handoff 重新声明；路径和动态 authority 属于新隔离任务，原 source_refs 只作为原候选元数据保留，未恢复原授权。见 [义务保存表](obligation_preservation.tsv)和 [三候选差分](candidate_comparison.json)。输入检查、依赖健康检查及失败 blocked、任务本地数据库、prepare/build/annotation/summary/报告与禁止伪造义务均未删除或降低。保留科学义务文本不代表已执行其检查或安装。

新回放走真实用户提交 → Consultant handoff parser/持久化 → Planner transport/parser → 九段准入（wire/bind、资源编译、句柄归一化、Plan contract、两轮 step effects/dataflow、candidate validation）→ 真实 PlanReview。P_min 的 compilation/dataflow receipt 均 compiled，六步正式持久化，结束于 **approval_pending**。没有预塞 approved/ready/succeeded；workspace/config 均 initially_available=false。完整请求、每段实际入参/结果、receipt、图事件与持久态见 [run02_main](replay/run02_main)。

隔离边界明确保留：继承 fixture 在新用户提交后设置 `intent_admission=managed_execution_admitted / decision_source=test_fixture` 和 schema2，本轮未验证 TaskRouter 的意图准入；Consultant 外部 preflight/运行能力观测被替代，继承 `_ensure_skill_runtime_ready` 替身不构成真实环境检查。模型叶子固定为捕获回复；人类读输入边界返回不可用，使正式 PlanReview 留待审批。新输入声明只用于机制验收，原科学路径仍作为未经访问的义务文本保留。不得将这些前置等同于真实科学能力或原 session 状态。

## 同入口差分与覆盖

| 候选 / 变体 | 当前真实准入结果 |
| --- | --- |
| 三份 P_live 原始业务字节，随后原始 STOP | 依次复现 1/5/1 项拒收；第四 STOP 正式 END，无 Plan。 |
| P_min 单字段修正 | 九段准入通过，六步 Plan 待审批。 |
| P_test 原业务字节在原 F6 上下文 | 通过，正式待审批。重建字节与旧 claim.raw_digest 相同；旧独立 raw 文件未留存，明确其重建来源。 |
| P_test 原业务字节直接放到现场输出上下文 | TASK_OUTPUT_MATERIALIZATION_MISSING，合理拒收。 |
| P_test 加现场五项 materializations、显式 result_contract_version=1、两个中间产物 required=true | 通过。仅加前两者会有三个 OPTIONAL_RESULT_REQUIRED_BY_CONSUMER；保留开发失败，不降 required 绕过。 |
| 全部 step_id 与 operation 不同且引用同步；受支持额外只读检查（同 capability 两步）；当前正式输入的合法 alias | 通过；无需固定六个步骤名。 |
| 缺 annotation role、错误现存步骤、错误父资源类型、未知 capability、额外 access、缺 config effect | 分别由真实原 gate 拒收；无执行。 |
| 模型试写 controller candidate_scope_id；引用另一个真实新任务正式分配的资源 | 分别 CONTROLLER_FIELD_FORBIDDEN / RESOURCE_REFERENCE_NOT_DECLARED；不将旧资源加入新 authority。 |
| 原 F6 fixture 的原命名与 prepare 步骤改名后引用同步 | 两例通过到待审批；本轮未运行改名后的下游六操作。 |

本轮最终覆盖由 20 个唯一新测试组成：2 个原链/P_min、14 个差分/反例、2 个 F6 反向准入、2 个旧断言见证。不同运行与精确字节复用分开记录，**不是一次新全套 F6 图通过**。其中 18 项是准入/停止测试通过，2 项 PASS 表示在 BASE/当前原 class 上都成功复现旧断言失败。开发回放自身的失败及修正原因保留，不计最终成功。详见 [verification.json](verification.json)与 replay 各 run。

## 请求足够性、加载与 F6 漏测

实际 system 明确 step_id 与 operation 分开、association 恰含三个字段、引用实际步骤、workspace 读输入和 config 产物规则；公开六项 capability 和完整前候选都在。**完整请求已有推导 P_min 所需核心信息，但结构化纠正信息有缺口。** READ_MISSING 的文字误导为补 association；SCOPE_INVALID 缺少查表未命中这一精确理由；公开递归投影删掉 step_id，修复目标退化为错误 message 内的间接线索。另有 skill_ids 示例错误使用 capability ID 占位符，现场三份候选实际 Skill ID 正确，因此不是本次触发原因。[逐规则和字段证据](request_and_versions/README.md)区分这些结论。

现场四次 Planner system 与最终 F6 正文逐字节相等（23880 bytes，SHA `f36b16645badb2e0d2a06c2cc42b72f2850405c1ab41373ccdc9ce15c17c32d8`），完整六能力列表相同，现场 Skill 正文与当前/final08 相同。20 个相关安全文件当前与 final08 相同；历史 runtime code hash 对比证实 9 个关键 callable 相同。没有历史全模块 globals/config 的完整加载证明；未捕获的投影模块加载版本仍 UNKNOWN，其实际投影结果有原请求证据。不能因此声称 F1—F6 未加载，也不能声称全部署加载完全一致。

旧 F6 的 provider 直接返回 `.plan()`，不从 messages 生成或纠正；step_id/operation、dependencies/role associations 同源填好，prepare config 和 annotation read 预先完整。原样 P_test 是默认结果模式，而现场显式 result_contract_version=1；现场 materialized 子输出也比原 fixture 多一层声明。旧下游夹具 `action/current_operation` 还依赖 step_id 是 OPS 成员，因此下次接线需明确分离字段。[人工字段表](f6_comparison/artificial_fields.tsv)、[覆盖盲区](f6_comparison/coverage_blindspots.tsv)直接对应原夹具，而非泛化方案。

## 保留的失败与后续

旧公共失败是 GenerateInvocationAudit.to_dict 整字典只允许四个 bool 的断言，实际还含三个既有调用身份字段；与 Plan 角色反馈无关。两版原 class 与断言逐字节/AST来源及两次失败见证见 [旧断言核验](request_and_versions/README.md)。未修改旧断言，不称原测试通过。旧九项 candidate scope fixture 失败继续保留，本轮未复跑。原 F1—F6 的固定模型/科学替身机制证据仍按其范围成立，本次现场科学任务未完成。

[原实施主单](../README.md)与 [原缺陷/验收表](../changes_and_acceptance.tsv)已追加本次精确遗漏。后续优先修既有诊断/公开投影及身份示例，再把现场修正版和正反变体接回既有 F6 六操作链；不扩大预算、不禁主动停止、不弱化 scope gate、不新增全局 validator/binder。未来真实 Planner-only 验收需另行授权，沿每 epoch 最多四次，记录全部尝试，禁止跨 session 反复抽到成功后只报一次成功。

审计范围结束：原始证据配对、首因、最小可满足修正、公开请求缺口、加载边界、受影响正反例和主单追加均明确。真实模型可靠性、改名候选下游六操作与真实科学验证尚未完成，本轮不以待审批替代这些结果。
