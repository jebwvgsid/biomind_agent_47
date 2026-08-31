# BioMind 权威状态契约与节点上下文架构 V2.2 规范

状态：Proposed 2.2（Control-plane / LLM-plane separation + Managed Artifact Contract）

目标流水线：Consultant -> TaskSpec -> Planner -> PlanSpec -> Review/Approval -> Generate -> Execute -> Validate -> Recovery/Completion

主要实现范围：`agent_core/task_spec.py`、`agent_core/plan_tracker.py`、`agent_core/consultation_state.py`、`agent_core/diagnostics.py`、`agent_core/recovery.py`、`agent_core/middleware.py`、`agent_core/completion_gate.py`、`bio_agents.py`、`supervisor.py`、`prompts/manager.py` 及聚焦回归测试。

关联基线文档：`decision_ownership_matrix.md`、`node_prompt_input_matrix.md`、`execution_provenance_model.md`、`incremental_migration_plan.md`、`snpeff_plan_contract_convergence_spec.md`。

2.2 修订边界：保留 2.1 已建立的 authority、identity、approval、audit 和 CAS 基础，但把 cryptographic identity、generation、CAS token、immutable object identity 和 projection fingerprint 从 LLM prompt/response 协议移回 Controller control plane；同时增加 Managed Artifact Contract，使已确定的 artifact 路径由 Controller/Renderer 确定性物化。

## 1. 执行决策

本次修改首先是一次**状态模型和契约架构重构**，其次才是 prompt/token 优化。

系统 MUST 建立一份唯一权威的当前任务语义 `TaskSpec`；Controller 为每次 Planner、Generate、RecoveryJudge、Supervisor 等调用创建内部 state binding，再向模型发送不同的确定性语义 payload。模型侧 payload MUST NOT 成为新的事实源，也 MUST NOT 通常暴露 cryptographic hash、generation、CAS token 或 immutable object ref。

> LLM reasons about semantics; Controller owns identity, concurrency and state consistency.

2.2 的五条中心不变量：

1. Cryptographic hashes、generations、CAS tokens、immutable object identities 和 projection fingerprints 是 control-plane metadata；Controller 必须记录和校验，但不得在正常业务 prompt 中暴露或要求 LLM 回传。
2. LLM-facing context 只包含 semantic facts、semantic handles、contracts、evidence excerpts 和 actionable violations。
3. Controller 在 dispatch 时内部绑定每次 LLM invocation，并在模型返回后验证、解析 handles 和 stamp identity。
4. Staleness 按节点相关的 semantic/execution domain 判断，不按无关 global generation 变化一票否决。
5. Managed artifact locations 由 Controller/Renderer 确定性物化；LLM 可以引用 semantic handles，但不得重定义 authoritative paths。

目标状态不是“每个节点收到字节完全相同的 TaskSpec”，而是：

> One authoritative semantic model, multiple deterministic non-authoritative LLM payloads, all bound internally by the Controller.

系统逻辑上分为五类持久状态和一类派生数据：

1. `Current TaskSpec`：当前仍然有效的用户事实、约束、输入和输出契约；
2. `PlanSpec`：如何完成当前任务，显式绑定 `TaskSpec` 身份；
3. `ApprovalSnapshot`：批准时不可变的任务与计划语义身份及历史决定；当前是否仍可使用由独立的 Approval Gate 动态计算；
4. `RuntimeState`：游标、状态、预算、执行尝试、清单和证据引用；
5. `Audit`：原始提交、来源、变更、旧值、决策和证据历史；
6. `Derived Node Context`：由 `ControllerBinding + optional ProjectionTrace + LLMPayload` 组成；只有 LLMPayload 进入模型，无权威写入权。

第一阶段 MUST 继续使用一个原子写入的 `plan_state.json` 作为持久控制面，不立即拆成多个物理 JSON 文件。只有逻辑边界、哈希绑定和迁移验证稳定后，才可单独提出物理拆分 RFC。

字符数、token 数和预算只能用于可观测性、异常告警与容量规划。任何节点都 MUST NOT 为满足固定字符上限而静默删除、截断或重新概括有效语义约束。

## 2. 问题陈述与证据基线

### 2.1 当前状态对象混合了不同生命周期的数据

当前 `plan_state.json` 同时保存当前任务语义、Consultant finalize 中间态、最终编译契约、计划、运行状态、批准字段和兼容投影。若这些对象未区分 `current`、`snapshot`、`derived` 和 `history` 生命周期，仅凭 JSON 内容相似进行删除，会造成 semantic aliasing：旧批准、旧计划或历史证据被最新 TaskSpec 重新解释。

因此，“内容相同”不是可删除的充分条件。删除或改为引用之前，必须先确认字段的生命周期和 authoritative consumer。

### 2.2 `session_1786335039` 的可复现体积证据

当前失败会话的持久状态：

`benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1786335039/plan_state.json`

已确认的紧凑 JSON 体积如下：

| 对象/字段 | 紧凑字符数 | 已确认问题 |
|---|---:|---|
| 整个 `plan_state.json` | 221,425 | 混合 current、runtime、history、projection |
| 顶层 `task_spec` | 65,172 | 包含大量来源、兼容别名和重复投影 |
| `task_state.task_spec` | 65,172 | 与顶层 `task_spec` 完全相等 |
| `task_state` | 97,855 | 内联 TaskSpec、consultation、audit、budgets |
| `consultation_state.final_contract` | 19,657 | 与 compiled contract artifact 完全相等 |
| `requirements` | 41,005 | 39 条要求重复携带来源与默认字段 |
| `expected_outputs` / `output_contracts` | 各 3,292 | 两者完全相等 |
| `facts` / `artifact_bindings` | 各 3,021 | 两者完全相等 |

另有以下已确认复制：

- `goal`、`request_envelope.request_text`、`raw_submission_text` 和首条 `user_directives[].text` 保存同一份原始请求；
- `normalization_audit` 中至少有两条完全相等记录；
- `consultation_state.final_contract` 与 finalize compiled artifact 是同一对象的内联副本；
- compiled contract 38 条语义约束与 TaskSpec 39 条 requirement 中的 38 条语义身份重叠；
- `normalize_requirement()` 当前通过 `{**raw, ...}` 保留原始整行，同时再次写入规范化来源、provenance、evidence 和默认字段。

在这一个会话中，已确认的精确复制/内联副本至少约 96.7k 紧凑字符；尚未计入“文本不同但语义等价”的重复。

### 2.3 当前节点 prompt 不是同一权威模型的确定性视图

运行捕获和实现审计表明：

- Planner 最终 prompt 约 42,280 字符，其中 TaskSpec 相关上下文约 27,362 字符；
- Generate 的 `_generate_task_spec_projection()` 约 53,184 字符，其中 `semantic_constraints` 约 38,903 字符；
- Planner 和 Generate 当前获取的是不同字段拼接出的 TaskSpec 投影；
- Recovery 先构建较宽上下文，再由 `RecoveryContext.prompt_view()` 按 32k 左右预算逐层裁剪，最后甚至只保留列表尾部若干项；
- `project_effective_user_facts()` 允许 mandatory sections 超出预算，但 optional sections 会因为预算整体消失；
- Planner repair 与初始 Planner、Generate、Recovery 对“任务事实”的可见性并不具有统一、可验证的约束闭包。

这说明当前问题不只是 Generate prompt 太长，而是同一事实被多次复制、转换、选择和截断，且不同节点缺少统一的 Controller source binding、payload policy 和覆盖证明。

## 3. 目标与非目标

### 3.1 目标

实现 MUST：

1. 建立唯一 canonical `CurrentTaskSpec`；
2. 区分当前状态、批准快照、运行状态、历史审计和派生投影；
3. 让 LLM 只提出变更，控制器验证并提交权威状态；
4. 让 `PlanSpec` 在控制面显式绑定一个 TaskSpec semantic identity，但不要求 Planner 回显该 identity；
5. 让批准绑定不可变的 TaskSpec/PlanSpec 身份，而不是“当前最新对象”的别名；
6. 为每个 LLM 节点生成确定性、可重建的语义 payload，并由 Controller 在模型外保存 state binding；
7. 取消因字符预算导致的有效语义静默截断；
8. 让 Recovery 按故障所有者路由到 Generate、Planner、Consultant/user 或内部协议阻断；
9. 让 CompletionGate 只从权威契约与执行证据判断完成，不再依赖兼容别名；
10. 保持 PlanTracker 为唯一持久控制面写入者，Controller 为唯一图路由写入者；
11. 通过幂等迁移支持旧 session/checkpoint，且不伪造批准和执行证据；
12. 在行为等价和恢复安全得到证明后，删除重复字段和旧投影；
13. 用指标证明 prompt 体积下降，但不把体积目标提升为业务语义约束。
14. 对 required/managed artifacts 使用语义 handle 和确定性路径物化，尽量在执行前消除文件名、目录和 quoting 漂移。

### 3.2 非目标

本规范不：

- 为 Generate 单独维护一套 LLM 摘要版 TaskSpec；
- 要求 Planner、Generate 和 Recovery 收到字节完全相同的 JSON；
- 规定 TaskSpec 必须低于 8k、10k 或 12k 字符；
- 通过只保留前 N 条/后 N 条约束来满足上下文预算；
- 把 audit 全部塞回每次 LLM prompt；
- 把批准快照合并为指向“最新 TaskSpec”的可变引用；
- 在第一阶段将单个 `plan_state.json` 拆成多个独立写入文件；
- 让 Planner 负责运行期图路由、重试预算或批准；
- 让 Generate 修改 TaskSpec、PlanSpec 或 Renderer schema；
- 要求 LLM 读取或抄回 hash、generation、approval ID、CAS token、content-addressed refs 或 projection fingerprint；
- 因没有现成专用 skill 而禁止 Planner 组合通用工具设计新流程；
- 改变生物信息学工具本身的科学逻辑。

## 4. 规范性术语和对象生命周期

本文中的 MUST、MUST NOT、SHOULD、SHOULD NOT、MAY 为规范性要求。

| 类别 | 含义 | 是否可变 | 是否权威 | 典型保留期 |
|---|---|---:|---:|---|
| Current state | 当前有效任务/计划/运行事实 | 是，只能通过受控 mutation | 是 | 当前 session |
| Snapshot | 某次批准或执行绑定时的身份 | 否 | 对该历史决定权威 | 审计保留期 |
| Derived node context | Controller binding、可选 trace 与 LLM 语义 payload | 可重建 | 否 | 一次调用或 trace 保留期 |
| Audit event | 谁在何时基于什么来源提出/提交了什么变更 | 追加式 | 对历史与 provenance 权威 | 审计保留期 |
| Evidence object | 日志、输入检查、执行、验证等较大对象 | 内容寻址后不可变 | 对其证据内容权威 | 审计/运行保留期 |
| Compatibility alias | 旧 schema 的只读迁移入口 | 否，禁止新写入 | 否 | 一个迁移窗口 |

`Snapshot` 与 `Current state` 即使在创建瞬间内容相同，也不是重复数据。前者必须固定历史身份，后者允许产生新 revision。

`Derived node context` 与 `Current state` 即使字段重叠，也不是第二份 TaskSpec。LLM payload 只能从权威对象生成，不能反向 merge 回 TaskSpec。

本文把 identity/fingerprint 分为三个等级：

- **A. Correctness-critical**：`task_semantic_hash`、`plan_semantic_hash` 和 immutable object content identity。Controller MUST 记录和校验；LLM 不可见。
- **B. Integrity/storage**：`task_record_hash`、`plan_record_hash`、audit/evidence object hashes 和 execution contract hashes。持久化/解析边界 MUST 校验；LLM 不可见。
- **C. Trace/observability**：`projection_semantic_hash`、`node_context_hash`。开启 trace、shadow verification 或 replay capture 时 SHOULD 记录；不得作为正常执行的必需业务 identity 或通用 stale gate。

增加 provenance 可以改变 record hash 和 `state_generation`，但只要 canonical semantic payload 不变，就不得改变 semantic hash、semantic revision 或批准适用性。

### 4.1 Hash domain 的规范定义

| Identity/fingerprint | 覆盖域 | 排除域与运行要求 |
|---|---|---|
| `task_semantic_hash` | canonical goal、typed inputs/bindings、typed constraints、output contracts、confirmed decisions、unresolved ambiguities，以及其他明确标记为 execution-semantic 的字段 | source/audit refs、timestamps、change history、record/generation metadata、所有 hash 字段 |
| `task_record_hash` | 完整 canonical TaskSpec record，包括 semantic payload、source/provenance/audit refs 和 record metadata | `task_record_hash` 自身、derived/cache 字段、外部 Runtime/Plan/Audit 正文 |
| `plan_semantic_hash` | steps、dependencies、bindings、success criteria、output ownership、pinned Renderer/Skill execution contract identities | validation status/violations/evidence/timestamp、audit refs、record metadata、所有 hash 字段 |
| `plan_record_hash` | 完整 canonical PlanSpec record，包括 semantic payload 和 PlanSpec 自身的 provenance/record metadata | `plan_record_hash` 自身、PlanValidationState、RuntimeState、derived/cache 字段 |
| `approval_scope_hash` | 仅在局部批准时覆盖被批准的 task/plan semantic subset | 全量批准时不作为独立必填 gate；audit/provenance-only refs、validation timestamp、runtime 状态必须排除 |
| `projection_semantic_hash` | projection type/version、selection policy、source semantic identities、included semantic handles 和 canonical semantic payload | SHOULD 仅用于 trace/shadow/replay，不进入 LLM，不作为正常调用必需项 |
| `node_context_hash` | 一次调用的完整内部 context，包括 semantic projection、runtime/evidence identity 和 binding metadata | SHOULD 仅用于 trace/shadow/replay，不进入 LLM，不作为通用 stale gate |

A/B 类 hash domain MUST 有独立 schema/version 和测试 fixture。向 semantic domain 新增字段是 approval-sensitive schema change，必须触发旧状态迁移/重新验证；不得通过某个通用“序列化整个 state”函数隐式决定 semantic identity。C 类 fingerprint 仅在启用相应观测功能时要求稳定和可回放。

### 4.2 Control-plane 与 LLM-plane 可见性

Cryptographic hashes、generations、CAS tokens、immutable object identities、approval refs 和 projection fingerprints 是 control-plane metadata。它们 MUST 由 Controller 记录、校验和在模型返回后 stamp 到 proposal/action/decision record，但 MUST NOT 通常进入 LLM prompt，也 MUST NOT 成为 LLM 输出字段。

LLM-facing context SHALL 只包含 semantic facts、semantic handles、contracts、evidence excerpts、actionable violations 和 allowed operations。模型可见的 `step.annotate`、`input.variant_vcf`、`output.final_fasta`、`constraint.database_source`、`E1` 等是有推理意义的调用内 handle，不是 SHA256、CAS ref 或持久对象地址。Controller 保存 alias map 并在模型返回后反向解析。

Task/Plan entity handles（如 `output.final_fasta`）应在一个 task semantic revision 内稳定；evidence/log handles（如 `E1`）可以是 invocation-local。两类 handle 都必须在本次 ControllerBinding 中唯一解析，并具有 type/direction/allowed-usage 信息；禁止同一字符串同时映射到不同对象或把未知 handle 猜测为相近对象。

## 5. 权威所有权模型

| 决策/数据 | 唯一权威 owner | 允许的提议者 | 禁止行为 |
|---|---|---|---|
| 当前用户语义 | TaskSpec + Controller commit | Consultant、用户输入解析器 | LLM 直接覆盖整份 TaskSpec |
| TaskSpec 规范化与 hash | TaskSpec compiler/controller | 无 | 节点各自重算不同 canonical hash |
| 科学路线与步骤契约 | PlanSpec/Planner proposal，经 validator 接受 | Planner | Generate/Recovery 改计划 |
| 计划验证事实 | PlanValidationState/validator | Planner Repair 仅消费 violations | validation metadata 写入 PlanSpec semantic payload |
| Renderer 参数 schema | Skill/Renderer registry | 无 | Planner/Generate 猜测参数 schema |
| Renderer 参数投影 | Controller materializer | Planner 提供语义引用 | LLM 自由文本成为绑定真源 |
| Managed artifact identity/path | Task/Plan contract + Controller/Renderer materializer | Planner/Generate 只引用 semantic handle | LLM 重定义 authoritative path |
| 历史批准决定 | immutable ApprovalSnapshot | Human Plan Review | 修改旧 snapshot 的 decision/status |
| 当前批准适用性 | Approval Gate derived result | 无 | 把 stale 状态写回 ApprovalSnapshot |
| 当前步骤、预算、尝试 | RuntimeState/PlanTracker | RecoveryPolicy 提议 outcome | 多节点直接自增预算或写 status |
| 执行事实 | ExecutionEvidence/provenance | Executor | LLM 生成成功事实 |
| Action validation | deterministic validator/Supervisor evidence | 可选 LLM reviewer 仅给意见 | reviewer 写图路由 |
| 步骤/任务完成 | CompletionGate 判定，PlanTracker 提交 | 无 | Generate 文本宣布完成即结束 |
| 恢复分类 | RecoveryPolicy | RecoveryJudge | RecoveryJudge 直接写状态 |
| 图转移 | Controller | 节点返回 typed outcome | 节点直接写 `next_step` |
| 历史与来源 | append-only Audit/Object store | 各 controller boundary | 修改旧事件或 ref 静默指向最新对象 |

## 6. 必须保持的全局不变量

### 6.1 身份和引用

1. 每份 canonical TaskSpec 必须有 `task_revision`、`task_semantic_hash`、`task_record_hash` 和 `canonicalization_version`。`task_revision` 只在 semantic payload 改变时增加。
2. 每份 PlanSpec 必须有 `base_task_revision`、`base_task_semantic_hash`、`plan_revision`、`plan_semantic_hash` 和 `plan_record_hash`。`plan_revision` 只在 plan semantic payload 改变时增加。
3. Task/Plan record 中 source/provenance/audit refs 的变化可以改变 record hash，但不得单独改变 semantic hash 或 semantic revision。
4. 每次节点调用的内部 `ControllerBinding` 必须声明 invocation/node identity、relevant semantic preconditions、必要的 integrity refs 和 node-specific staleness domain；这些字段不进入 LLM payload。
5. 开启 trace/shadow/replay 时，`ProjectionTrace` SHOULD 声明 projection type/version、selection policy、included semantic handles 和可选 trace fingerprints；它不进入 LLM payload，也不参与业务 proposal schema。
6. ref 必须指向不可变、内容寻址的对象；解析时必须校验 hash。
7. ref 解析失败、hash 不符或 revision 不存在时，必须产生 `INTERNAL_STATE_REFERENCE_ERROR`，禁止静默 fallback 到 latest。
8. semantic payload 与完整 record 的 canonical serialization 域必须分别定义、固定键排序、Unicode、数字和空值策略，并版本化；不同节点不得自行实现哈希序列化。
9. constraint ID 初期继续保留当前完整稳定 ID。未经单独 identity migration 设计，禁止直接改为 revision-local 的 `c01/c02`。
10. 同一个稳定 constraint ID 的语义值发生变化时必须产生新 TaskSpec revision；引用方通过 `task_semantic_hash` 确定其含义。

### 6.2 写入和权限

11. LLM 只能返回 semantic proposal/delta，不得返回一份被直接接受为权威状态的完整 TaskSpec，也不得被要求回显 hash、generation、approval identity 或 CAS token。
12. Controller 在 dispatch 时捕获 `ControllerBinding`；模型返回后按 node-specific semantic/execution preconditions 校验 staleness，再把内部 identity stamp 到 proposal record。无关的 audit/provenance/telemetry generation 变化不得自动废弃 LLM 结果。
13. Planner 只能写 PlanSpec proposal，不能修改 TaskSpec。
14. Generate 只能写 action proposal，不能修改 TaskSpec、PlanSpec、批准或预算。
15. RecoveryJudge 只能写 recovery decision proposal，不能直接写状态或图路由。
16. Supervisor 只能产生 validation evidence/review opinion，不能批准计划、增加预算或写 `next_step`。
17. CompletionGate 只返回完成判定；只有 PlanTracker/Controller 可以提交步骤或任务状态。

### 6.3 语义完整性

18. 节点视图可以比完整 TaskSpec 小，但必须是基于显式引用和依赖闭包的确定性选择，不是新的自然语言总结。
19. 视图不得因为字符预算删除 required/hard/global/current-step dependency constraints。
20. 若目标模型上下文无法容纳必要闭包，调用必须失败为 `CONTEXT_CAPACITY_EXCEEDED`，并记录缺口；不得带不完整语义继续执行。
21. 体积异常必须指出贡献最大的字段、constraint/source IDs 和构建阶段，不得只记录总字符数。
22. Audit 不默认进入 prompt；Controller 只能按 node policy dereference 与当前决定相关的 history refs，并向 LLMPayload 提供 semantic handle/excerpt。Raw `audit_refs` 不进入模型。
23. 原始用户提交只在 raw input audit 中保存一次。TaskSpec 保存已规范化的当前事实及 source refs，不重复内联整段原文。

### 6.4 批准和执行

24. 批准必须固定 TaskSpec 与 PlanSpec 的 semantic identity，并可附带批准时的 record hashes 供历史完整性核验；批准适用性不得绑定 audit-only 字段。
25. ApprovalSnapshot 只记录不可变历史决定 `decision=approved`，不得包含会随后改写为 stale 的 mutable status。
26. Approval Gate 必须动态产生 `applicable`、`stale_task`、`stale_plan`、`scope_mismatch`、`missing` 或 `unverifiable` 之一；该结果属于 derived/runtime gate state，不属于 snapshot。
27. 任何 approval-sensitive TaskSpec 或 PlanSpec semantic payload 变更都必须使旧批准变为不适用。
28. 旧批准不适用后，Generate 和 Execute 必须被 approval gate 阻止。
29. 运行状态或 provenance-only 更新不能改变批准快照内容，也不能单独使批准不适用。
30. 计划步骤、Renderer/Skill execution contract、artifact ownership、输入绑定或成功标准改变时必须重新验证；是否重审由 impact policy 决定，但默认 hard/required 变化必须重审。
31. CompletionGate 必须验证当前 TaskSpec/PlanSpec semantic identities 仍等于批准快照，且所有 required outputs 和 evidence 对应当前运行尝试。

### 6.5 原子性

32. 每次持久状态提交必须形成单调递增的 `state_generation`；它表示整个控制状态的 commit version，不表示所有被引用对象的出生版本。
33. 每个 committed generation 必须形成内部一致、可验证的引用集合。Immutable Task/Plan records、ApprovalSnapshot、Audit 和 Evidence 可以创建于更早的 `created_generation`，无需在每次 runtime 更新时重新物化。
34. 当前 generation 中的所有 refs、hash bindings、created generation 和 approval applicability 必须可验证；旧对象不得因被新 generation 引用而改变内容或身份。
35. Phase 1 继续通过单文件临时写入、flush/fsync、原子 replace 提交 `plan_state.json`。
36. 大型 immutable audit/evidence 对象应先写入 object store 并验证，再由一次状态提交挂接 ref。
37. generation re-check 与 atomic replace 必须位于同一个 exclusive writer critical section；多进程可能写入时必须使用 OS/file lock 或等价 single-writer mechanism，只有进程内锁不足以构成 CAS。
38. 进程崩溃后只允许看到旧 generation 或完整的新 generation，不能看到半更新状态。

## 7. Phase 1 逻辑状态 Schema

第一阶段建议在现有 `plan_state.json` 内迁移到如下逻辑结构。字段名可在实现时小幅调整，但生命周期、authority 和 hash binding 不得改变。

```json
{
  "schema_version": 8,
  "state_generation": 42,
  "task_id": "session_...",
  "current_task": {
    "task_spec": {},
    "task_revision": 7,
    "task_semantic_hash": "sha256:...",
    "task_record_hash": "sha256:...",
    "canonicalization_version": "task-c14n-v2",
    "created_generation": 39,
    "last_mutation_event_id": "audit:..."
  },
  "current_plan": {
    "plan_spec": {},
    "plan_revision": 4,
    "plan_semantic_hash": "sha256:...",
    "plan_record_hash": "sha256:...",
    "base_task_revision": 7,
    "base_task_semantic_hash": "sha256:...",
    "renderer_contract_bindings": [],
    "skill_execution_contract_bindings": [],
    "created_generation": 40,
    "last_mutation_event_id": "audit:..."
  },
  "plan_validation_state": {
    "status": "valid",
    "validated_plan_semantic_hash": "sha256:...",
    "validator_version": "plan-validator-v2",
    "violations": [],
    "validation_evidence_refs": [],
    "validated_at": "..."
  },
  "approval_snapshot": {
    "approval_id": "approval-...",
    "decision": "approved",
    "approved_task_revision": 7,
    "approved_task_semantic_hash": "sha256:...",
    "approved_task_record_hash": "sha256:...",
    "approved_plan_revision": 4,
    "approved_plan_semantic_hash": "sha256:...",
    "approved_plan_record_hash": "sha256:...",
    "approval_scope": {
      "mode": "full",
      "scope_hash": null
    },
    "diagnostic_fingerprints": {
      "execution_contract_hashes": []
    },
    "created_generation": 40,
    "approved_at": "...",
    "approval_source": "human_plan_review",
    "review_event_id": "audit:..."
  },
  "runtime_state": {
    "status": "running",
    "current_step_id": "step-...",
    "current_stage_id": "stage-...",
    "budgets": {},
    "pending_action": {},
    "provenance_state": {},
    "manifest_refs": [],
    "evidence_refs": [],
    "recovery_state": {},
    "approval_gate_result": {
      "applicability": "applicable",
      "evaluated_generation": 42,
      "approval_id": "approval-...",
      "reason_codes": []
    }
  },
  "audit_index": {
    "raw_submission_ref": "object:sha256:...",
    "audit_head": "audit:sha256:...",
    "event_count": 1937,
    "latest_by_type": {
      "task_mutation": "audit:sha256:...",
      "plan_mutation": "audit:sha256:...",
      "approval": "audit:sha256:...",
      "runtime_event": "audit:sha256:..."
    }
  },
  "compatibility": {
    "source_schema_version": 7,
    "migrations_applied": [],
    "read_aliases_enabled": true
  }
}
```

### 7.1 `CurrentTaskSpec` 的职责

TaskSpec 只保存**当前有效、执行决策所需的规范化事实**：

- task identity 和 revision；
- 简洁、规范化的目标；
- typed inputs/artifact bindings；
- typed constraints/requirements；
- typed output contracts；
- 当前 confirmed decisions；
- 当前 unresolved ambiguities；
- 必要的 source/audit refs；
- selected capability intent（若它属于当前用户/Consultant 语义，而非运行时 activation）。

TaskSpec identity 分为：

- `task_semantic_hash`：对目标、typed inputs、typed constraints、output contracts、confirmed decisions 和 unresolved ambiguities 等执行语义字段计算；
- `task_record_hash`：对完整 canonical TaskSpec record 计算，包括 source/audit refs；
- `task_revision`：只跟随 `task_semantic_hash` 变化。仅增加 provenance 时 record hash 和 state generation 变化，task revision 与 semantic hash 保持不变。

每个 required input/output artifact 还必须有 stable artifact identity、可供节点使用的 semantic handle、direction/format、path policy 和 validation contract。`managed_exact` artifact 的 canonical path 是 TaskSpec/Controller 权威事实，不是 LLMPayload 中的自由文本参数。

TaskSpec MUST NOT 内联保存：

- 完整原始用户全文的多个副本；
- change log 全历史；
- Consultant finalize 中间 coverage/repair 对象；
- `final_contract` 的第二份内联副本；
- runtime cursor、retry budgets、pending action、execution evidence；
- approval record；
- Planner/Generate/Recovery 专用自然语言摘要；
- 与 canonical 字段完全相等的兼容别名。

### 7.2 TaskSpec 字段收敛规则

| 当前字段组 | 目标处理 | 权威保留位置 |
|---|---|---|
| `goal` / request text / raw submission / first directive text | canonical goal 保留一份；原文只保留 raw audit ref | TaskSpec + Audit |
| `requirements` / compiled constraints | 归一为一套 typed constraints；来源改为 refs | TaskSpec |
| `expected_outputs` / `output_contracts` | 合并为一套 output contracts | TaskSpec |
| `facts` / `artifact_bindings` | 合并为 typed artifact/input bindings | TaskSpec |
| `change_log` | 移出当前对象，追加到 audit | Audit |
| `semantic_compilation` 中间数据 | 编译结果保留 canonical constraints；过程移出 | TaskSpec + Audit |
| `effective_user_facts` | 改为确定性 projection，不持久为第二真源 | Derived View |
| `task_state.task_spec` | 只读迁移 alias 后删除 | Compatibility |
| `consultation_state.final_contract` | 改为 compiled artifact ref；已提交语义进入 TaskSpec | Audit/Object store |
| coverage/finalize context | 内容寻址保存，通过 refs 查阅 | Audit/Object store |

字段移除必须以 consumer audit 为前置条件。某字段若是 CompletionGate、Renderer projection、旧 session migration 或 approval scope 的实际 authoritative input，必须先迁移 consumer，再停止新写，最后才能删除。

### 7.3 `PlanSpec` 的职责

PlanSpec 保存完成任务的批准候选路线：

- plan identity/revision、`plan_semantic_hash` 和 `plan_record_hash`；
- `base_task_revision` 和 `base_task_semantic_hash`；
- 有序步骤及 dependencies；
- 每步引用的 constraint IDs；
- input/output artifact IDs；
- `skill_id` 及 `skill_execution_contract_version/hash`；
- `renderer_id` 及 `renderer_contract_version/hash`；
- renderer invocation contract identity 或 controller-materialized contract；
- success criteria；
- output ownership。

PlanSpec 不复制整份 TaskSpec，不保存原始用户全文，不保存 runtime attempt/status 历史，也不保存 validation status、timestamp、violations 或 validation evidence refs。计划中的语义必须通过 stable IDs + pinned `task_semantic_hash` 解释。

PlanSpec 对 Task-managed artifacts 只保存 stable artifact reference/semantic handle 和 producer-consumer relation，不复制或重定义 canonical path。只有 path policy 显式为 `model_selectable` 的临时 artifact 才允许计划/动作提出路径，并仍需 Controller 规范化与安全验证。

`plan_semantic_hash` 只覆盖步骤、依赖、bindings、success criteria、output ownership、Renderer contract identity 和会影响执行语义的 Skill contract identity。`plan_record_hash` 覆盖完整 PlanSpec record。Validator 新增 evidence 或时间戳不得改变任一 PlanSpec hash；Plan validation 是独立状态。

每个 execution contract binding 至少包含 `contract_id`、`contract_version`、`contract_hash` 和可选 immutable `contract_ref`。执行和验证必须按 hash 解析 exact contract，不能把相同 ID 的 registry latest 当作等价对象。

### 7.4 `PlanValidationState` 的职责

PlanValidationState 描述“validator 对某份计划检查出了什么”，至少包含：

- `validated_plan_semantic_hash`；
- validator schema/version；
- validation status；
- structured violations；
- validation evidence refs；
- validation timestamp 和 convergence information。

PlanValidationState 可随重新验证更新，但不得写回 PlanSpec semantic payload。Approval Gate 只接受绑定当前 `plan_semantic_hash` 且 validator version 满足当前 policy 的有效结果。

### 7.5 `ApprovalSnapshot` 的职责

ApprovalSnapshot 是不可变历史身份，不是当前对象别名。至少固定：

- approval ID/source/time/review event 和不可变的 `decision=approved`；
- approved task revision/semantic hash/record hash/canonicalization version；
- approved plan revision/semantic hash/record hash；
- 若为局部批准，则固定 scope definition 和 `approval_scope_hash`。

canonical constraint、input binding、Renderer invocation、output ownership、Skill contract 和 Review projection 等组件 fingerprints MAY 作为 diagnostic diff 保存，用于解释“哪里变了”；在这些内容已被 task/plan semantic hashes 覆盖的全量批准中，它们不得成为重复的必填 Gate identity。

当前 `approval_record`/`approval_scope` 可作为迁移基础，但 MUST 将 TaskSpec identity 提升为一等批准键，并禁止“重算当前 scope 后仍沿用旧 approval”的隐式行为。

Snapshot 中批准时的 record hashes 用于还原“当时审阅的精确记录”，但 Approval Gate 的适用性以 semantic hashes 和 approval-sensitive scope 为准。后续 provenance-only record 变化不得改写 snapshot，也不得单独使其 stale。

`approval_snapshot` 表示当前控制状态引用的最近一次批准对象。创建新批准时必须创建新的 immutable snapshot/object 并更新 current pointer；旧 snapshot 继续由 approval audit/event ref 保存，禁止原位覆盖旧对象。

当前适用性由独立 gate 动态计算，结果为 `applicable`、`stale_task`、`stale_plan`、`scope_mismatch`、`missing` 或 `unverifiable`。该结果可保存在 RuntimeState 的 bounded derived cache 中，但每次进入 Generate/Execute/Completion 前必须针对当前 semantic identities 和必要 scope 重新校验。Generation 仅用于判断缓存是否需要重算，不能因无关 generation 变化直接得出 stale。

### 7.6 `RuntimeState` 的职责

RuntimeState 只保存运行中的可变控制面：

- task status、execution status；
- current step/stage/cursor；
- typed budgets 和 counters；
- pending action；
- execution/action/attempt/provenance identity；
- manifest、artifact、validation、completion evidence refs；
- recovery state 和 recent attempt refs；
- last transition。

RuntimeState 通过 task/plan semantic hashes 绑定当前执行语义，但不复制 TaskSpec 或 PlanSpec。其 `approval_gate_result` 是可重建的当前适用性结果，不修改 ApprovalSnapshot。

### 7.7 `Audit` 与 TaskSpec 的区别

TaskSpec 回答：**现在什么是真的？**

Audit 回答：**谁、在什么时候、基于什么来源，把什么从旧值改成了新值，控制器为何接受或拒绝？**

二者可能包含相同值的片段，但生命周期和用途不同。Audit 应追加式、不可变、按需读取；TaskSpec 应紧凑、当前、可直接用于确定性决策。Audit 不能成为正常执行每一步都必须 dereference 的隐藏主状态。

## 8. TaskSpec Mutation 协议

### 8.1 Consultant 输入

Consultant 读取：

- 当前 `ConsultantLLMPayload` 中的规范化目标、facts、constraints、artifacts 和 semantic handles；
- 本轮新用户消息；
- formal input audit 的语义结果；
- 候选 capabilities/skills；
- 与当前澄清有关的 targeted evidence excerpts 及调用内 handles；
- 允许的 mutation schema。

Consultant 不需要读取完整旧 change log、所有 execution history、整个 session 对话、raw audit/object refs，也不得看到或回传 current task hashes/generation。Controller 在 dispatch 时单独保存 `ControllerBinding` 和 handle alias map。

### 8.2 Consultant 输出

Consultant MUST 输出 mutation proposal，而非整份 TaskSpec：

```json
{
  "proposal_type": "task_spec_mutation",
  "schema_version": 1,
  "operations": [
    {
      "op": "upsert_constraint",
      "constraint_id": "required_behavior.database_source",
      "value": "custom_genome",
      "reason": "explicit_user_instruction",
      "supporting_evidence": ["U1"]
    }
  ],
  "unresolved_ambiguities": [],
  "proposal_evidence_refs": []
}
```

允许的操作必须为 closed enum，例如 `set_goal`、`upsert_input`、`upsert_constraint`、`remove_constraint`、`upsert_output_contract`、`confirm_decision`、`mark_ambiguity`。自由 JSON merge patch 不得作为默认协议。

`U1`、`E1` 等 supporting handles 只在本次调用中有意义。Controller 负责将其解析回新用户消息或 immutable evidence object；模型不得返回持久 ref、content hash 或 source generation。

### 8.3 Controller commit

Controller 在 dispatch 时创建 `ControllerBinding`，保存调用所基于的 semantic preconditions、integrity metadata、handle alias map 和 touched entity versions。模型返回后依次执行：

1. 校验 semantic proposal schema、operation allowlist 和 handle resolution；
2. 按 Consultant staleness domain 比较当前 task semantic identity 与 touched entity preconditions；
3. 若只有无关 audit/provenance/telemetry 导致 generation 或 record hash 变化，在最新 record 上重新验证并继续，不重新调用 LLM；
4. 若 relevant task semantics 已变化，返回 `STALE_TASK_SEMANTICS`，不得把 proposal 盲目应用到新语义；
5. 在 exclusive writer critical section 内重新读取 current state，对最新 record 应用 closed operations；
6. 解析 supporting handles，补充 Controller-owned source/audit refs；
7. 运行 ontology、input/output、conflict 和 provenance validation；
8. canonicalize candidate TaskSpec，分别计算 candidate semantic hash 与 record hash；
9. 若 semantic hash 不变但 record hash 改变，提交新的完整 record 和 audit event，但不增加 task revision，也不使 approval non-applicable；若两个 hash 都不变，记录 accepted/rejected true no-op；
10. 若 semantic hash 变化，增加 task revision，并计算对 PlanSpec、ApprovalSnapshot applicability 和 RuntimeState 的 impact；
11. 先写 audit/evidence objects，再通过存储层 CAS 原子提交下一 generation；
12. Controller 将 invocation binding 和 commit identities stamp 到持久 proposal/commit record，LLM 从不负责这些字段。

存储层 CAS 仍可因为 concurrent writer 返回冲突，但 Controller MUST 在锁内重新读取并判断冲突属于哪个 staleness domain。仅 record/generation 变化不得直接触发再次调用模型。

### 8.3.1 Node-specific staleness domains

| 节点/操作 | Correctness preconditions | 不应单独判 stale 的变化 |
|---|---|---|
| Consultant semantic mutation | task semantic identity；必要时 touched entity version | unrelated audit/provenance/telemetry、record-only change |
| Planner | task semantic identity | record-only change、unrelated runtime/evidence |
| Planner Repair | task semantic identity、plan semantic identity、violation-set identity | validation timestamp、audit append、unrelated evidence |
| Generate | task semantic identity、plan semantic identity、approval applicability、current step ID、execution epoch | unrelated audit、archive、other-step telemetry |
| RecoveryJudge | task/plan semantic identities、failure fingerprint、attempt/execution identity | unrelated audit、other-attempt evidence |
| Supervisor reviewer | step/action/attempt identity、relevant semantic constraints、evidence identity | unrelated task history或其他步骤 evidence |
| Approval Gate | task/plan semantic identities；局部批准时 scope identity | record-only change、runtime telemetry |
| Audit append/storage commit | generation、record integrity、audit head | 不适用；这是 control-plane CAS domain |

若 correctness precondition 变化，Controller 应产生 typed stale result 并按 owner 路由；不得无界重复调用同一节点。若仅无关 metadata 变化，Controller 应重新解析 handles、重新验证 proposal 并继续。

Controller 必须以 `node_type + relevant semantic/execution identities + stale reason` 构造 stale signature。同一 signature 不得重复触发等价 rerender；只有 relevant identity 确实形成新版本时才可创建新的 invocation。被无关 metadata 变化成功 revalidate 的 proposal 不消耗 LLM retry budget；真正 stale 的旧响应不消耗 execution retry budget，但其 reroute/redispatch 必须进入独立 bounded stale/convergence audit。

### 8.4 变更影响分类

| 变化类型 | Plan 处理 | Approval 处理 | Runtime 处理 |
|---|---|---|---|
| 仅补充非执行性 provenance/ref | semantic hash/revision 不变，record hash 变化；Plan 可保持 | applicability 不变 | 业务状态不变，但 generation/audit head 更新 |
| 文案变化但 canonical semantics 不变 | 不修订 TaskSpec 或记录 no-op | 保持 | 不变 |
| required/hard constraint、input、output contract 变化 | 必须 impact analysis/replan | 失效 | 未执行步骤重置；已执行证据保留但不得误用 |
| optional preference 变化 | validator 判断受影响步骤 | 默认重新 review 受影响范围 | 受影响步骤 stale |
| unresolved ambiguity 被解决 | 重算绑定与计划 | 若执行语义变化则失效 | 相应步骤 stale |
| audit-only 追加 | Task/Plan identities 不变 | applicable | 业务状态不变，但 generation/audit head 更新 |

## 9. 确定性节点视图协议

### 9.1 三对象调用协议

ViewBuilder 必须把一次节点调用拆成三个对象：`ControllerBinding`、可选 `ProjectionTrace` 和 `LLMPayload`。只有第三个对象进入模型 prompt。

```text
Authoritative State -> ViewBuilder
  -> ControllerBinding ------------------------------+
  -> ProjectionTrace (optional, no LLM)              |
  -> LLMPayload -> LLM -> Semantic Proposal ----------+
                                                        -> Validate / Stamp / Commit
```

#### 9.1.1 `ControllerBinding`——只在控制面

```json
{
  "invocation_id": "inv-...",
  "node_type": "generate",
  "state_generation": 42,
  "task_semantic_hash": "sha256:...",
  "task_record_hash": "sha256:...",
  "plan_semantic_hash": "sha256:...",
  "plan_record_hash": "sha256:...",
  "approval_ref": "object:sha256:...",
  "step_id": "step-3",
  "execution_epoch": 5,
  "staleness_domain": "generate-v1",
  "semantic_handle_map_ref": "object:sha256:..."
}
```

ControllerBinding 记录权威状态、并发 guard、approval、attempt/failure identity 和 semantic handle alias map。它 MUST NOT 被拼入 system/human message；模型响应也不得被要求包含 `invocation_id`。Controller 通过调用栈或 invocation registry 将响应与 binding 关联。

Binding 必须在 dispatch 前注册且对该 invocation 不可变。同步调用可以保存在进程内 bounded registry；可能跨进程、异步恢复或进程崩溃后接收响应的调用必须在 dispatch 前持久化 immutable binding/ref。没有 active binding、binding 已终结或响应被重复消费时必须拒绝，禁止从模型正文猜测其所属 task/step。调用成功、失败或 stale 后应终结 binding；长期 trace 仅保存必要 ref/摘要，不让 registry 无界增长。

#### 9.1.2 `ProjectionTrace`——仅用于 debug/shadow/replay

```json
{
  "projection_type": "generate_task_view",
  "projection_version": "generate-v2",
  "selection_policy": "current-step-closure-v2",
  "included_constraint_handles": [],
  "included_artifact_handles": [],
  "projection_semantic_hash": "sha256:...",
  "node_context_hash": "sha256:..."
}
```

ProjectionTrace SHOULD 在 trace/shadow/replay 开启时记录；关闭时系统仍必须可以正确运行。两个 fingerprints 都是 observability 数据，不进入 LLM，不作为 LLM response schema 或正常业务 gate 的必要条件。

#### 9.1.3 `LLMPayload`——真正进入模型

```json
{
  "task": {
    "goal": "...",
    "constraints": [],
    "inputs": [],
    "outputs": []
  },
  "current_step": {},
  "runtime": {},
  "feedback": {},
  "evidence": [],
  "allowed_operations": []
}
```

LLMPayload 只包含语义事实、semantic handles、必要 contract 内容、bounded evidence excerpts、actionable violations 和 allowed operations。它不得包含 cryptographic hash、global generation、approval/content-addressed ref、CAS token、projection fingerprint 或无推理意义的内部 UUID。

#### 9.1.4 构建和响应规则

- 同一 authoritative semantic state、node policy 和 relevant execution context 必须生成语义等价的 LLMPayload；
- 每个 payload semantic handle 必须在 ControllerBinding alias map 中唯一解析；
- LLM 可返回 semantic handles，但不得返回其后端 UUID/hash/ref；
- Controller 在响应后解析 handles、执行 node-specific stale 校验、验证 proposal，并 stamp control metadata；
- 不允许 LLM 摘要被持久化为新的 TaskSpec/PlanSpec 真源；
- payload 缓存按 relevant semantic/execution domain 管理，不得用 global generation 作为所有节点的唯一 cache key；
- 缓存不匹配时重建，不 fallback 到旧 payload；
- 若 trace 开启，ProjectionTrace 必须可以说明选择了哪些 semantic handles，但不把内部 ref 列表重新注入 prompt。

### 9.2 约束闭包

`GenerateLLMPayload`、`RecoveryLLMPayload` 等较小语义 payload 必须通过显式闭包选择：

```text
current step referenced constraints
  + dependency step contracts required by current inputs
  + task-global hard/required constraints
  + relevant input/output bindings
  + renderer/skill contract dependencies
  + approval-sensitive invariants
```

构建器必须在控制面保留 included stable identities 与 LLMPayload handles 的映射。可选地在 ProjectionTrace 记录 `excluded_handles_with_reason`，但不得把内部 IDs、refs 或所有被排除对象正文重新内联进 prompt。

闭包完整性由测试和 validator 保证，不由字符数决定。若闭包异常大，应报告造成膨胀的 constraint/source 行，而不是截掉列表尾部。

### 9.3 节点可见性矩阵

| 节点 | LLMPayload 必须可见 | 可按需加入 LLMPayload | 不应进入 LLM prompt/response |
|---|---|---|---|
| Consultant | 当前语义 facts/constraints/artifacts、新用户消息、input audit 结果、候选 capabilities、mutation schema | 相关 evidence excerpts 和 `U1/E1` handles | hashes、generation、raw refs、完整 runtime history、旧 TaskSpec 副本 |
| Planner | goal、constraints、managed artifact handles、output contracts、capability/renderer 语义说明、plan schema | targeted feasibility evidence excerpts | hashes、generation、registry object refs、raw history、重复 requirements/outputs |
| Planner Repair | 与初始 Planner 等价的语义 payload、当前 Plan 内容、actionable violations、allowed mutation scope | violation 相关 contract/evidence excerpts | base hashes/generation、PlanValidationState 内部 refs、TaskSpec 修改权 |
| Plan Review | 人类可读 task/plan mapping、validation result、semantic diff、approval impact | constraint/evidence source 摘要 | cryptographic identities（除显式诊断视图）、runtime logs 全量 |
| Generate | current-step semantic closure、managed input/output handles、active capability instructions、allowed actions、runtime/progress/feedback | 当前失败所需 `E1/E2` excerpts | task/plan/approval hashes、generation、absolute managed paths、整份 PlanSpec/历史 |
| RecoveryJudge | current step/action contract、failure/attempt facts、evidence handles/excerpts、recent strategies | targeted log excerpts | object hashes/refs、global generation、load-all-then-tail-truncate、整个 history |
| Supervisor reviewer | current step constraints、executed action、validation evidence handles/excerpts | relevant scientific criteria | task/plan hashes、重新解释用户总体意图、写路由 |
| Deterministic Controller/Renderer/Execute/Gates | 不适用；这些组件读取 control-plane binding/authoritative state | 不适用 | 不得为了方便把内部对象先转成 prompt 再让 LLM 判定 |
| SessionArchive summarizer（若调用 LLM） | bounded historical facts 和 semantic handles | targeted event excerpts | current-state fallback、CAS refs、完整 immutable object graph |

所有 LLM 节点均遵循同一条规则：semantic handles 可见，cryptographic/control metadata 不可见。只有用户明确请求诊断 identity 时，才可在独立诊断回答中展示必要的短 fingerprint；该例外不得改变模型业务协议。

### 9.4 PlannerLLMPayload

PlannerLLMPayload 应包含：

- canonical goal；
- 全部有效 hard/required constraints；
- typed inputs/artifacts；
- output contracts；
- confirmed decisions/unresolved ambiguities；
- capability requirements；
- 有推理意义的 semantic handles。

它不应重复包含 raw request、`effective_user_facts`、第二份 `requirements`、第二份 `expected_outputs` 或同一 skill catalog 的多种格式。

Planner 输出只包含 PlanSpec semantic proposal，不声明或回显 base hash/generation。Controller 使用 dispatch 时保存的 binding 对 proposal 做 staleness 校验，并自动 stamp `base_task_semantic_hash` 等持久 identity。Planner 发现任务语义不完整时返回 typed `TASK_SEMANTICS_INCOMPLETE`，由 Controller 路由至 Consultant/clarification；Planner 不补写 TaskSpec。

### 9.5 GenerateLLMPayload

GenerateLLMPayload 应只展开当前步骤及其确定性闭包，同时包含：

- 当前步骤引用的 constraints；
- global hard/required constraints；
- relevant managed input/output semantic handles；
- active skill/renderer 的语义能力和使用规则；
- current renderer/action contract；
- 当前 runtime progress 与结构化 feedback。

Renderer 与 Skill execution contract 的 exact ID/version/hash 只保存在 ControllerBinding/PlanSpec control plane。Generate 看到的是完成当前动作所需的语义规则、allowed actions 和 managed artifact handles，不看到 contract hash。

Generate 不读取完整 PlanSpec。`CurrentStepContext` 由 Controller 从 pinned PlanSpec 构建，并包含足以验证 action proposal 的 exact contract。

### 9.6 RecoveryLLMPayload

RecoveryLLMPayload 应以错误为中心，包含：

- 当前步骤及 constraint closure；
- exact action/runtime/manifest contract；
- 最新 execution/validation/completion evidence；
- recent attempts 和已经尝试的策略；
- progress/no-progress facts；
- targeted log/evidence excerpts 与 `E1/E2` semantic handles。

必须替换当前 `prompt_view()` 的“先加载完整对象，再按字符串/list 上限逐层缩小”策略。日志正文仍可有明确的 head/tail bounded excerpt；ControllerBinding/trace 保留 source ref、完整日志位置和 truncation metadata，LLMPayload 只显示 excerpt 与 semantic handle。语义约束本身不得按 list tail 裁剪。

### 9.7 ReviewLLMPayload

ReviewLLMPayload 面向 Plan Review 和 Supervisor：

- 以人类可读形式展示 canonical facts 与 plan mapping；
- 每个步骤显示 constraint IDs、input/output IDs、success criteria 和 owner；
- 展示 current-versus-approved diff；
- 展示 validation violations 与影响；
- 不重新抽取或解释用户原文。

## 10. Planner、验证、修复与批准契约

### 10.1 初始 Planner

Planner 接收 `PlannerLLMPayload` 与 capabilities/renderer 的语义 contract，输出只含步骤、依赖、semantic handles、成功标准和 capability selection 的 PlanSpec semantic proposal。Controller 自动绑定/stamp dispatch identity，并校验：

- dispatch 后 task semantic precondition 仍成立；无关 record/generation 变化按 §8.3 重新验证而不重调 Planner；
- step schema/dependencies；
- 每个 semantic reference 唯一解析；
- managed input/output ownership 和 path policy；
- Renderer projection completeness；
- success criteria 与 CompletionGate 可判定性；
- skills/capabilities 可用性，以及 Renderer/Skill execution contract version/hash pinning；
- 不存在越权字段。

### 10.2 Plan validator

Validator 必须返回结构化 violation，而非仅返回自然语言：

```json
{
  "violation_handle": "V1",
  "code": "MISSING_RENDERER_BINDING",
  "severity": "blocking",
  "owner": "controller_projection",
  "step_handle": "step.annotate",
  "constraint_handles": [],
  "field_paths": [],
  "evidence_handles": [],
  "repairable_by": ["controller", "planner"],
  "message": "..."
}
```

owner 必须决定修复路由。传给 LLM 的 violation 使用 semantic handles；持久 violation ID、evidence refs 和 violation-set fingerprint 留在 ControllerBinding/PlanValidationState。Planner 不得被要求修复 controller-owned renderer binding；Controller 不得把缺失的科学选择伪装成 renderer 格式问题。

### 10.3 Planner Repair

Planner Repair 必须看到：

- 与初始 Planner 语义等价的 Planner LLMPayload；
- 当前 PlanSpec；
- actionable violations 及 semantic handles；
- allowed mutation scope；
- renderer/plan 的相关语义 contract。

Planner Repair 只返回使用 semantic handles 的 PlanSpec delta，不读取或回显任何 base identity。ControllerBinding 内部固定 task semantic、plan semantic 和 violation-set identities；模型返回后仅这些 relevant preconditions 变化才返回 `STALE_PLAN_REPAIR_BASE`。Validation timestamp、audit append 或无关 evidence 变化应重新验证后继续。Controller 对 delta 应用字段级权限检查；越权修改返回 `PLAN_REPAIR_SCOPE_VIOLATION`；语义 no-op 使用独立 convergence budget，且不得清空未解决 violations。Validator 的重新运行只更新 PlanValidationState，不产生 PlanSpec delta。

### 10.4 Review 与批准

Plan Review 输出人类可读计划和 validation summary。批准时创建 immutable ApprovalSnapshot。

Approval Gate 每次进入 Generate/Execute 前必须动态计算 applicability：

```text
snapshot exists and decision == approved
AND current_task_semantic_hash == approval.approved_task_semantic_hash
AND current_plan_semantic_hash == approval.approved_plan_semantic_hash
AND PlanValidationState.validated_plan_semantic_hash == current_plan_semantic_hash
AND current validation is complete and non-blocking
AND (approval mode == full OR partial approval scope recomputation matches snapshot)
```

结果必须为 `applicable`、`stale_task`、`stale_plan`、`scope_mismatch`、`missing` 或 `unverifiable` 之一。任何非 `applicable` 结果均不得继续执行。Task/Plan record-only 差异不单独导致 stale，但 ref/hash 无法验证时必须为 `unverifiable`。不得使用文本相似、相同 plan revision number 或 legacy `approved_plan_hash` 单字段代替完整验证，也不得修改旧 ApprovalSnapshot 来表达 stale。

## 11. Generate、Renderer、执行和完成契约

### 11.1 Generate

Generate 依据 `GenerateLLMPayload`（其中已包含 CurrentStepContext 的语义内容）提出 semantic action proposal。模型输出只包含：

- action kind/source/role；
- argv/script/structured renderer operation；
- managed input/output semantic handles；
- expected evidence handles；
- semantic recovery intent（若为 probe/retry）。

模型不得携带 task/plan/approval hashes、generation、action UUID、contract hash 或 CAS metadata。Controller 在返回后通过 ControllerBinding 校验 relevant staleness domain，生成 action identity，并 stamp task/plan/approval/action contract identities。

推荐 action schema：

```json
{
  "action_kind": "command",
  "argv": [
    "tool",
    "{input:source_fasta}",
    "-o",
    "{output:final_fasta}"
  ],
  "outputs": ["output.final_fasta"],
  "expected_evidence": ["artifact.output.final_fasta"]
}
```

Pre-action validator 在执行前解析 semantic handles、校验 allowed action 和 current step/execution epoch。Relevant execution semantics 已过期时拒绝 action；无关 audit/generation 变化不得单独导致重调 Generate。

### 11.2 Managed Artifact Contract

对于 TaskSpec/PlanSpec 已确定的 required artifact，artifact identity、authoritative path、filename policy、producer ownership 和 validation contract 属于 Controller/Renderer control plane，不属于 Generate 的自由文本决策。

内部 binding 至少包含：

```json
{
  "artifact_id": "artifact-uuid-...",
  "semantic_handle": "output.final_fasta",
  "direction": "output",
  "format": "fasta",
  "path_policy": "managed_exact",
  "canonical_path": "/output/aa.fa",
  "required_basename": "aa.fa",
  "producer_step_id": "step-...",
  "relocatable": false,
  "validation_contract_id": "fasta-output-v1"
}
```

LLMPayload 只展示 `output.final_fasta`、格式、用途和占位符语法，不展示后端 artifact UUID/hash；对于 `managed_exact` 路径，默认也不展示绝对路径。Controller 将：

```text
{output:final_fasta} -> /output/aa.fa
{input:source_fasta} -> verified input binding
```

确定性物化到 argv、Renderer arguments 或受控环境变量。

Managed artifact 规则：

1. PlanSpec 通过 semantic artifact handle 引用 TaskSpec binding，不得复制或重定义 path；
2. Generate 对 `managed_exact` artifact MUST 使用 handle/placeholder，不得直接选择或重打 authoritative path；
3. Controller 必须在执行前完成 placeholder resolution、normalization、allowlist 和 ownership 校验；
4. 结构化 argv 优先于自由 shell 文本；需要脚本时使用 typed placeholder/env injection，禁止未经 shell-aware escaping 的字符串替换；
5. 缺失、未知、重复或方向错误的 handle 在执行前失败；
6. 迁移期 legacy literal path 仅在与 authoritative binding 精确一致并通过完整 validation 时 MAY 接受，且必须记录 compatibility 命中；新 session 不得产生 literal managed paths；
7. path policy 真正允许模型选择临时/scratch artifact 时，必须显式标记 `model_selectable`，不得把 required final output 默认为可选路径。

### 11.3 Renderer

Renderer materializer 是确定性组件，只读取：

- renderer registry schema/version；
- pinned current step `renderer_contract_id/version/hash`；
- pinned `skill_execution_contract_id/version/hash`（若 Skill 影响执行语义）；
- verified TaskSpec/PlanSpec bindings；
- outdir/output ownership；
- runtime allowlist。

Renderer 不读取用户原文，不依赖 Generate 自由文本猜测参数。缺失绑定产生 typed violation 并按 owner 路由。

### 11.4 Pre-action、Execute 与三层 output 验证

输出验证必须区分：

1. **Declared contract**：Task/Plan 声明的 semantic artifact、authoritative path、producer 和 criteria；
2. **Proposed/materialized action**：Generate 返回的 handles，以及 Controller 物化后的 exact argv/Renderer invocation；
3. **Observed artifact**：当前 producer attempt 实际生成、签名并发现的路径和内容。

Pre-action validator 在第 2 层比较 handles、materialized paths 与 declared contract。任何 mismatch 必须在执行前产生 typed violation，包含 semantic handle、expected、proposed/materialized 和 owner；不得留到通用 Recovery 才发现。

Execute 只运行已通过 pre-action validation 的 action，并产生 immutable attempt-scoped ExecutionEvidence。继续沿用现有 provenance identity chain：

```text
plan_id -> step_id -> action_id -> attempt_id/execution_id -> artifact_version_id
```

Post-execution validator 在第 3 层比较 observed artifacts 与 declared/materialized contract。文件存在不等于成功；artifact 必须能追溯到当前 producer attempt，并经 content/format/scientific validation 后才可成为 valid evidence。

### 11.5 Artifact relocation

发现非声明路径的文件时不得简单 `mv` 到目标位置并宣布完成。Artifact relocation 仅在以下条件全部满足时 MAY 使用：

1. 源文件属于当前 producer attempt；
2. 内容验证满足目标 artifact contract；
3. 它是当前 attempt 唯一且可证明的 intended output；
4. relocation 不改变科学语义、格式或压缩/index 配套关系；
5. output contract 明确允许 relocation；
6. Controller 在移动前后记录 artifact lineage、signatures、source path、destination path 和 relocation reason。

任一条件不满足时必须重新执行正确 materialized action，不能利用工作目录中的任意同扩展名文件补齐 output。

### 11.6 CompletionGate

CompletionGate 是确定性边界，读取：

- current identities 与 ApprovalSnapshot；
- 当前步骤 contract；
- manifest/renderer stages；
- action execution evidence；
- artifact versions 和 validation；
- success criteria；
- TaskSpec output contracts；
- blocker/recovery state。

CompletionGate 不读取 `expected_outputs`/`facts` 等 legacy alias，不扫描消息历史，不把 LLM 的 `done` 文本当作完成证据。

Task 完成必须同时满足：

1. Approval Gate applicability 为 `applicable`，且 snapshot decision 仍为不可变的 `approved`；
2. 所有 required steps 完成；
3. 所有 required outputs 由当前 lineage 生成并验证；
4. manifest/stage 状态一致；
5. success criteria 已有证据；
6. 任何 relocation 都满足 §11.5 并有完整 before/after lineage；
7. 无 blocking validation/recovery/internal protocol error。

## 12. Recovery 分类与路由

RecoveryJudge/Policy 必须先判定 failure owner，再选择下一阶段：

| failure owner/type | 典型问题 | 路由 |
|---|---|---|
| `execution_runtime` | 命令失败、依赖/runtime drift、可修复脚本问题 | Generate retry/patch |
| `action_generation` | action 未使用 managed handle、literal path/declared output 与 step contract 不符 | Pre-action fail -> Generate repair |
| `observed_artifact_mismatch` | materialized action 正确但工具实际输出路径/集合不符 | RecoveryPolicy |
| `plan_binding` | 步骤依赖、artifact mapping、Plan semantic refs 错误 | Planner Repair |
| `task_semantics` | 用户约束缺失、真实歧义、输入角色不确定 | Consultant；确需决定时 ask user |
| `approval_stale` | task/plan 已变化、approval scope 不匹配 | Plan validation/review/approval |
| `controller_projection` | Renderer/controller binding 生成错误 | deterministic controller repair 或 internal block |
| `internal_state_reference` | ref 不存在、hash/generation 撕裂 | `INTERNAL_STATE_REFERENCE_ERROR`，禁止问用户修 schema |
| `capability_unavailable` | 所需能力在允许环境中真实不存在 | `CAPABILITY_UNAVAILABLE`，提供缺失能力证据 |

### 12.1 Output mismatch ownership

同一个“期望 `aa.fa`、出现 `bb.fa`”必须按首次发生偏差的层级分类：

| Task contract | Plan binding | Proposed/materialized action | Observed result | owner | route |
|---|---|---|---|---|---|
| `output.final_fasta -> aa.fa` | 错误绑定/重定义为 `bb.fa` | 未执行 | 未执行 | `plan_binding` | Planner Repair |
| `output.final_fasta -> aa.fa` | 正确引用 handle | Generate 写 literal `bb.fa` 或未引用 handle | 未执行 | `action_generation` | Pre-action fail -> Generate |
| `output.final_fasta -> aa.fa` | 正确 | proposal handle 正确，但 Controller/Renderer 物化为 `bb.fa` | 未执行 | `controller_projection` | deterministic controller repair/internal block |
| `output.final_fasta -> aa.fa` | 正确 | materialized path 为 `aa.fa` | 工具实际生成 `bb.fa` | `execution_runtime` / `observed_artifact_mismatch` | RecoveryPolicy |

`action_generation` 的标准 violation：

```json
{
  "code": "ACTION_OUTPUT_BINDING_MISMATCH",
  "owner": "action_generation",
  "artifact_handle": "output.final_fasta",
  "required_filename": "aa.fa",
  "proposed": "bb.fa",
  "route": "generate"
}
```

Generate 的 repair LLMPayload 只需说明 proposed、required semantic handle、required filename/placeholder 和 allowed scope，例如：“你的动作写入 `bb.fa`；本步骤必须引用 managed output `output.final_fasta`。请使用 `{output:final_fasta}`，不得修改计划或输出契约。”不得附加 task/plan hashes。

Recovery 不能把每种失败都返回 Generate，也不能因为上下文被截断而声称用户约束缺失。`requires_user_input=true` 只能用于 source-bound 的真实用户决策；内部协议、schema、projection、hash 或路由问题不得转嫁给用户。

没有新专用 skill 不构成自动硬阻断。如果通用 shell/Python/R、已安装工具或现有 renderer 能满足 contract，Planner MAY 设计新工作流；只有 capability audit 证明所需能力确实不可用时才返回 `CAPABILITY_UNAVAILABLE`。

## 13. Audit 与内容寻址对象存储

### 13.1 Audit event 最小结构

```json
{
  "event_id": "audit:sha256:...",
  "previous_event_ref": "audit:sha256:...",
  "event_type": "task_mutation_committed",
  "task_id": "...",
  "state_generation_before": 41,
  "state_generation_after": 42,
  "base_task_semantic_hash": "sha256:...",
  "base_task_record_hash": "sha256:...",
  "result_task_semantic_hash": "sha256:...",
  "result_task_record_hash": "sha256:...",
  "actor": "consultant_controller",
  "source_refs": [],
  "operation_refs": [],
  "evidence_refs": [],
  "timestamp": "..."
}
```

Audit event 采用 append-only/content-addressed 语义，并通过 `previous_event_ref` 形成可验证的单向事件链。当前 state 只保存 bounded `audit_head`、`event_count` 和 `latest_by_type`，不得内联从任务开始以来的全部 event refs。完整索引由 `audit/index` 或 event chain 维护，不依赖目录遍历、文件名排序或无界数组。

链式提交必须保证 `previous_event_ref` 等于提交前的 `audit_head`，新事件的 generation before/after 与状态提交一致，提交成功后 `audit_head` 指向新事件。链断裂、event count 不连续或 generation 跳变必须产生 corruption diagnostic，不得静默跳过缺失事件。

对于大型原始请求、finalize context、coverage、compiled contract、execution log 和 validation detail，应保存不可变 object，并在状态与 audit 中仅保留 ref、hash、schema/type、size 和必要摘要。

### 13.2 ref 解析规则

- ref 必须包含或可确定 content hash；
- object 写入后必须校验 hash 和 schema；
- object 不得原位修改；
- 同 hash 可去重；
- 读取者必须声明需要的 object type；
- type/hash/schema 不符时显式失败；
- audit/object store 不得静默使用同名最新文件；
- NodeContextBuilder 只能在控制面 dereference 明确列入该 node policy 的 refs，并向 LLMPayload 投影 semantic handle/excerpt；不得把 raw ref/hash 直接拼入 prompt。

### 13.3 隐私和容量

审计应保存必要 provenance，但不得保存 secret、完整环境变量或无界内存转储。原始 prompt/object 可以较大，但每次 LLM 调用只能按决策需要选取；“持久保存”不意味着“每次完整注入”。

## 14. 原子提交和并发模型

### 14.1 Phase 1 提交流程

```text
load generation N
  -> acquire exclusive writer lock
  -> re-read current generation M under lock
  -> evaluate ControllerBinding node-specific staleness domain
  -> if relevant semantics changed: typed STALE_*
  -> if only unrelated metadata changed: re-resolve handles and build candidate on M
  -> canonicalize and validate all cross-object bindings
  -> persist immutable audit/evidence objects
  -> set state_generation = N + 1
  -> write plan_state.json.tmp
  -> flush + fsync file
  -> atomic replace plan_state.json
  -> fsync parent directory where supported
  -> release writer lock
```

提交时必须使用 compare-and-swap 语义：current-state re-read、node-specific stale evaluation、candidate validation 与 replace 必须处于同一个 exclusive writer critical section。若写入永远严格限制在单进程，可使用 PlanTracker process-local serialized writer；只要存在多进程、resume 进程或外部 writer 的可能，就必须使用 OS/file lock 或等价的跨进程 single-writer mechanism。

若磁盘 generation/record integrity 相对 dispatch binding 已变化，Controller 首先分类变化；relevant semantic/execution precondition 改变才返回 typed stale，无关 audit/provenance/telemetry 变化则在当前 generation 上重建 candidate 并继续，不重新调用 LLM。若竞争发生在本次锁外准备和实际 commit 之间，存储 CAS 可以重试 control-plane commit，但不得把每次 CAS conflict 自动升级为 LLM rerender。仅在加锁前读取 N、加锁外构建、replace 前不重新检查的实现不构成 CAS。

### 14.2 为什么 Phase 1 不拆文件

如果立即将 `task_spec.json`、`plan_spec.json`、`runtime_state.json` 分开写入，就必须先实现 transaction manifest、generation pointer、崩溃恢复和跨文件 CAS。当前已有 `_atomic_write_plan_state()` 可作为单文件原子边界，因此先做逻辑分层能降低迁移风险。

只有满足以下条件后，Phase 2 才可考虑物理拆分：

- 所有 cross-object identity 已 hash-bound；
- 单文件 schema 迁移与回放稳定；
- object store/ref checker 已上线；
- 有故障注入测试证明任意写入点崩溃不会产生 torn generation；
- 有明确性能证据证明单文件成为瓶颈。

## 15. 当前消费者迁移审计

删除字段前，实施者 MUST 生成机器可审计的 consumer inventory，至少覆盖：

| 当前来源/别名 | 需要核查的消费者 | 目标真源 |
|---|---|---|
| 顶层 `task_spec` | Planner、Generate、CompletionGate、approval scope、wiki/context lineage | `current_task.task_spec` |
| `task_state.task_spec` | resume/wiki/context lineage/compat paths | 只读 migration alias -> `current_task.task_spec` |
| `effective_user_facts` | Planner、Generate、Recovery prompt builders | deterministic LLMPayload projections |
| `requirements`/`semantic_constraints` | TaskSpec normalizer、Planner validator、renderer projection | canonical typed constraints |
| `expected_outputs`/`output_contracts` | Planner、CompletionGate、Review | canonical output contracts |
| `facts`/`artifact_bindings` | formal resolver、renderer projection、CompletionGate | canonical bindings |
| `consultation_state.final_contract` | finalize resume/repair/diagnostics | compiled contract object ref + committed TaskSpec |
| `approval_record`/`approval_scope`/`approved_plan_hash` | Generate gate、Execute gate、CompletionGate | immutable ApprovalSnapshot + derived ApprovalGate result |
| `validation_issues`/renderer projection issues/validation timestamps | Plan Review、Planner Repair、approval gate | PlanValidationState，PlanSpec 只保留计划本身 |
| plan_state 中累计 audit ref arrays | resume、diagnostics、archive | bounded `audit_head/event_count/latest_by_type` + external index/event chain |
| Recovery `task_spec_summary` + `effective_user_facts` | RecoveryContext builder/prompt | RecoveryLLMPayload + ControllerBinding |
| prompt 中的 task/plan hashes、generation、approval refs | Planner/Repair/Generate/Recovery prompt builders | ControllerBinding；LLMPayload 中删除 |
| action 中 literal required output paths | Generate/parser/renderer materializer | managed artifact handles + deterministic materialization |
| message history fallback | router、direct chat、legacy recovery | explicit node inputs/audit refs |

迁移必须遵守顺序：建立新真源 -> 双读对比 -> consumer 切换 -> 停止旧字段新写 -> replay 验证 -> 删除旧字段。禁止先删除再依靠运行时异常发现隐藏依赖。

## 16. 实现映射

### 16.1 `agent_core/task_spec.py`

- 定义 canonical TaskSpec vNext schema；
- 实现共享 canonical serializer，并分别定义 semantic payload hash 与完整 record hash；
- 将 `normalize_requirement()` 从 `{**raw, ...}` 改为明确字段 allowlist；
- 将 provenance/evidence 正文改为 refs；
- 合并 output/fact aliases；
- 实现 typed mutation schema、candidate application 和 semantic no-op detection；
- 实现 node semantic closure 和 LLMPayload projection；cryptographic binding 不进入该 payload；
- 将当前 `project_effective_user_facts()` 标为 compatibility adapter，最终删除其独立事实模型职责。

### 16.2 `agent_core/consultation_state.py` 与 `agent_core/consultant_finalize.py`

- Consultant/finalize 只输出 semantic TaskSpec mutation；Controller 在 dispatch/commit 边界保存并 stamp generation 与 semantic/record identities；
- compiled contract、coverage、context 作为 immutable artifacts；
- `final_contract` 不再内联复制到 durable current state；
- finalize repair 的 LLM schema 不含 base identities；ControllerBinding 负责 staleness 和原子提交；
- 保留旧 finalize session 的只读迁移能力。

### 16.3 `agent_core/plan_tracker.py`

- 升级 plan schema，加入 `state_generation` 和逻辑分区；
- 保持唯一 durable control-plane writer；
- 抽象 `commit_state_transition(expected_generation, expected_hashes, mutation)`；
- 扩展 `_atomic_write_plan_state()` 的 durability/CAS 测试，并在 generation re-check 与 replace 外围建立真实 exclusive writer critical section；
- TaskSpec/PlanSpec 分别维护 semantic/record hashes，PlanSpec 明确绑定 TaskSpec semantic identity；
- 将 PlanValidationState 从 PlanSpec semantic payload 分离；
- ApprovalSnapshot 固定 task+plan semantic identities 和批准时 record hashes，当前 applicability 由 gate 派生；
- current state 使用 bounded `audit_head/event_count/latest_by_type`，不保存无界 event ref 数组；
- 将 `task_state.task_spec`、旧 approval 字段设为迁移期只读 aliases；
- 提供 impact analysis、approval applicability 计算和 stale step marking；
- 提供 node-specific staleness evaluator，区分 semantic/execution stale 与 unrelated record/generation changes；
- 不破坏现有 execution provenance identity chain。

### 16.4 `bio_agents.py`

- 用统一 NodeContextBuilder 替代 Planner/Generate 各自拼接 TaskSpec，并产出 `ControllerBinding + optional ProjectionTrace + LLMPayload`；
- 替换 `_generate_task_spec_projection()`；
- 为 Consultant、Planner、Planner Repair、Generate、Recovery、Review 创建 exact LLMPayload 和调用内 semantic-handle map；
- `projection_semantic_hash` 与 `node_context_hash` 仅在 trace/shadow/replay 开启时生成；
- 删除 LLM 输出 schema 中的 hash/generation/approval/ref 字段；Controller 在响应后 stamp；
- 维护 bounded invocation registry 和 stale-signature dedupe，拒绝 unknown/terminated/duplicate invocation response；
- 每次 action/generate/execute 前动态计算 approval applicability；
- Controller 负责 mutation commit、view 构建、failure-owner route；
- 移除从多个 `values/task_state/plan_state` 猜测 authoritative TaskSpec 的 fallback 链；
- 保留一个版本窗口的显式 compatibility read，并记录命中指标。

### 16.5 `agent_core/diagnostics.py`、`agent_core/recovery.py`、`agent_core/middleware.py`

- `RecoveryContext` 在控制面引用 authoritative identities，Recovery LLMPayload 只包含 failure/attempt facts、semantic handles 和 evidence excerpts；
- 语义约束不再通过 generic list-tail compaction；
- 日志使用 bounded excerpt + immutable ref；
- recovery LLM decision schema 只包含 owner/type、semantic repair intent 和 evidence handles；Controller 解析 handles 并 stamp base identities/evidence refs；
- 共用一个 RecoveryPolicy；
- Controller 将 typed outcome 映射到图节点；
- internal state/ref failure 不得 ask user。

### 16.6 `prompts/manager.py`

- prompt 模板只描述节点职责和输出协议；
- 不再在不同模板中复制互相冲突的 TaskSpec 定义；
- 明确 LLMPayload 是 non-authoritative semantic context，禁止放入或要求回显 control-plane metadata；
- Planner Repair 接收 allowed mutation scope；
- Generate 不得改 Task/Plan；
- RecoveryJudge 必须先分类 owner。

### 16.7 Managed artifact 实现边界

- `agent_core/task_spec.py`/formal resolver 建立 canonical artifact binding 和 path policy；
- Plan parser/validator 强制步骤只通过 semantic artifact handles 绑定 required artifacts；
- Generate action schema 支持 typed `{input:...}` / `{output:...}` placeholders 和 declared output handles；
- Controller/Renderer materializer 负责 exact path resolution、argv/env safe injection、ownership 和 compatibility policy；
- pre-action validator 比较 declared、proposed/materialized 层并在执行前产生 owner-aware mismatch；
- post-execution/provenance 比较 observed artifact 并记录 producer attempt；
- CompletionGate 只接受 lineage-valid、contract-valid 的 managed artifact version。

### 16.8 `supervisor.py` 和 `agent_core/completion_gate.py`

- Supervisor reviewer 读取 ReviewLLMPayload 的当前步骤闭包；
- Supervisor 只产出 validation evidence；
- CompletionGate 读取 canonical output contracts、ApprovalSnapshot 和 provenance；
- 删除对 legacy outputs/facts/task summaries 的执行期依赖；
- gate result 保持 typed、deterministic、可回放。

### 16.9 `agent_core/skill_manager.py` 与 Renderer registry

- 为 Renderer contract 和会影响执行语义的 Skill execution contract 建立稳定 ID、version 和 canonical hash；
- PlanSpec 固定这些 contract identities，而不只保存 `renderer_id`/`skill_id`；
- 执行时必须解析 exact pinned contract hash：若旧版本仍可按 hash 解析，则继续使用被批准版本；若同 ID 只剩不同内容或 pinned object 不可得，则 Plan validation/approval applicability 必须为 stale/unverifiable，禁止同 ID 静默改用新行为；
- 纯描述性 skill metadata 变化不得误算为 execution semantic change。

### 16.10 SessionArchive / context lineage

- 保存 immutable snapshots、audit refs 和 bounded human-readable summaries；
- 不把 archive summary 当 current TaskSpec fallback；
- 恢复时先迁移并验证 current state，再按 ref 取历史；
- 旧 session 缺少 object 时产生明确 migration diagnostic，不伪造 object。

## 17. 分阶段交付计划

实施 MUST 从 Phase 0 开始。Phase 0 不改变行为，早期阶段只建立新真源/新调用边界并进行 shadow 对比；在 consumer inventory、semantic behavior、LLMPayload coverage 和 incident replay 尚未通过前，禁止删除任何旧字段或关闭 compatibility read。

### Phase 0：基线与可观测性

不改变行为，记录每个节点最终 view/prompt 的：

- source semantic/record hashes 和 generation；
- 当前哪些 cryptographic/generation/ref 字段实际进入了各 LLM prompt/response；
- trace 开启时的 projection type/version、projection semantic hash 与 node context hash；
- included IDs 数量；
- section 字符/token 贡献；
- exact duplicate bytes、semantic alias 命中；
- compatibility fallback 命中；
- audit dereference 次数与体积。

成功门：`session_1786335039` 的当前体积、重复关系、control-metadata prompt leakage 和 node-specific visibility 可由自动测试/报告复现；Phase 0 不删除字段、不改变路由。

### Phase 1：Canonical TaskSpec 和 mutation 边界

- 定义 TaskSpec vNext、semantic payload/record canonicalization 和双 hash；
- Consultant 只输出 semantic mutation；ControllerBinding 保存调用 identity；
- Controller 提交；
- provenance/history 移至 refs；
- 尚不删除旧字段，双写/双读对比。

成功门：新旧 TaskSpec 对 incident fixtures 产生等价的 canonical semantic contract；模型输出不含 base identity；Controller 可确定性判定 mutation stale/no-op/conflict。

### Phase 2：PlanSpec 绑定和 ApprovalSnapshot

- PlanSpec 固定 base task semantic identity，并维护 plan semantic/record hashes；
- PlanValidationState 与 PlanSpec 分离；
- Planner/Repair 只消费/返回 semantic plan 内容、violations 和 scope；Controller 自动 stamp exact identities；
- 批准固定 task+plan semantic identities、批准时 record hashes 和 Renderer/Skill contract identities；
- ApprovalSnapshot 只记录 immutable decision，gate 动态计算 applicability；
- task/plan semantic 变化触发 impact/stale applicability，record-only 变化不触发重审。

成功门：旧批准不能跨语义变化沿用；audit-only 变化不会让 Approval Gate 误判为 non-applicable，且不会改写历史 snapshot。

### Phase 3：Control-plane / LLM-plane separation

- 上线统一 NodeContextBuilder，生成 ControllerBinding、optional ProjectionTrace 和 LLMPayload；
- 为每个 LLM 节点定义 staleness domain 和 semantic-handle alias map；
- 建立 bounded invocation registry、late/duplicate response rejection 和 stale-signature dedupe；
- shadow 比较旧 prompt facts 与新 LLMPayload closure；
- 从 prompt/response schema 删除 hashes、generation、approval/content refs 和 trace fingerprints；
- 先切 Consultant/Planner/Repair，再切 Generate/Recovery/Supervisor。

成功门：所有 required semantic handles 在对应节点闭包中；LLM prompt/response 无 control-plane metadata；unrelated generation/record change 不废弃可用 proposal；不存在语义 tail truncation。Trace 关闭时业务行为仍完全可用。

### Phase 4：Managed Artifact Contract

- canonical artifact bindings 增加 path policy、producer、semantic handle 和 validation contract；
- Plan 只引用 managed artifact handles；
- Generate action 使用 typed placeholders；
- Controller/Renderer 确定性 materialize exact path；
- pre-action 和 post-execution 分别验证 declared、proposed/materialized、observed 三层；
- 上线 output mismatch owner routing 和受限 relocation policy。

成功门：`aa.fa/bb.fa` fixtures 在执行前或正确 owner 边界被拦截；新 session 的 required managed outputs 不由 LLM literal path 决定。

### Phase 5：Recovery 与 CompletionGate 收敛

- failure owner 分类与统一 RecoveryPolicy；
- CompletionGate 切换 canonical contracts；
- internal protocol errors 显式阻断；
- generic capability 路径可用。

成功门：execution/plan/task/approval/internal failures 路由正确；用户不会被要求修复内部 schema。

### Phase 6：停止旧字段写入并删除副本

- consumer inventory 全部切换后停止写 aliases；
- migration reader 保留一个发布窗口；
- 删除 `task_state.task_spec`、duplicate outputs/facts、inline final contract 和旧 effective facts persistence；
- 更新 schema docs 和 fixtures。

成功门：新 session 不产生这些副本；旧 session 可幂等迁移；核心 replay 全部通过。

### Phase 7（可选）：物理对象拆分

只有第 14.2 节条件满足并有性能证据时启动单独 RFC。该阶段不属于本规范的首轮实现完成条件。

## 18. 测试要求

### 18.1 Canonicalization 与 mutation

- 相同语义、不同 JSON key order 产生相同 semantic hash；相同完整 record、不同 key order 产生相同 record hash；
- canonicalization version 变化显式可见；
- relevant task semantic/touched-entity precondition 变化拒绝；
- 只有 unrelated audit/provenance 导致 generation/record hash 变化时，Controller 在最新 record 上重新验证并继续，不重调 Consultant；
- allowed operations 成功；越权 field 拒绝；
- semantic no-op 不增加 revision；
- provenance/source-ref 变化只改变 task record hash 和 generation，不改变 task semantic hash/revision/approval applicability；
- true audit-only event 不改变 Task/Plan 两类 hash，只推进 generation/audit head；
- constraint 值变化改变 task semantic/record hashes 并增加 revision；
- ref hash/type/schema 错误显式失败。

### 18.2 Node context 与 LLMPayload

- ControllerBinding 包含 node-specific correctness/integrity preconditions，但最终 LLM messages 中不存在 hash、generation、approval ref、CAS token 或 projection fingerprint；
- Consultant/Planner/Repair/Generate/Recovery/Supervisor 的响应 schema 不要求回显上述字段；
- semantic handles 在 LLMPayload 中唯一、可读，并由 Controller alias map 无歧义解析回 stable identity；
- Planner LLMPayload 覆盖所有 active required/hard constraints；
- Generate LLMPayload 覆盖 current-step refs、dependencies、global constraints 和 managed artifact handles；
- Recovery LLMPayload 覆盖失败判定所需 contract/evidence excerpts；
- LLMPayload 不含第二套 raw request/requirements/outputs；
- 超出模型容量返回 `CONTEXT_CAPACITY_EXCEEDED`，不产生部分视图；
- 日志 excerpt 在 LLMPayload 中有 evidence handle，ref/offset/truncated metadata 留在 ControllerBinding/trace；
- 任何 LLMPayload 无法反向修改 TaskSpec；
- trace 关闭时节点行为不变；trace 开启时相同 semantic sources/policy 的 `projection_semantic_hash` 稳定，runtime/evidence 变化只影响 `node_context_hash`。
- Planner dispatch 后只追加 audit/provenance：proposal 重新验证后接受，LLM 调用次数不增加；
- Planner dispatch 后 task semantics 改变：返回 `STALE_TASK_SEMANTICS`；
- Planner Repair 的 violation set 不变但 validation timestamp 改变：delta 仍可验证；violation set 改变：typed stale；
- Generate dispatch 后 other-step telemetry 改变：action 可继续；current step 或 execution epoch 改变：typed stale；
- Recovery dispatch 后无关 audit 改变：decision 可继续；failure/attempt identity 改变：typed stale。
- 相同 stale signature 不重复触发等价 rerender；新 relevant identity 最多创建一个新的 active invocation，重复/迟到响应被拒绝。

### 18.3 Plan 与批准

- Planner proposal 不含 base identities；Controller 自动 stamp dispatch task identity；
- task semantic identity 改变时 Planner proposal stale；仅 record/generation 改变时重新验证并继续；
- Planner Repair 超 scope 拒绝；
- no-op repair 不清空 violations；
- validation status/timestamp/evidence refs 只改变 PlanValidationState，不改变 plan semantic/record hashes；
- approval 固定 task+plan semantic identities 和批准时 record hashes；
- ApprovalSnapshot 的 `decision=approved` 在 current state 变化后仍完全不变；
- required constraint/input/output、Renderer contract hash 或 execution-semantic Skill contract hash 变化使 gate 返回 stale/scope mismatch；
- provenance/audit-only record 变化不使批准变为不适用；
- `stale_task`、`stale_plan`、`scope_mismatch`、`missing`、`unverifiable` 均阻止 Generate 和 Execute；
- snapshot 内容在 current state 变化后仍可按批准时 record hash 恢复。

### 18.4 Managed Artifact Contract

- Task `output.final_fasta -> /output/aa.fa`、Plan 引用 `output.final_fasta`、Generate proposal 使用 `{output:final_fasta}`：Controller 正确物化并通过 pre-action validation；
- Generate 直接提出 `bb.fa`：执行前返回 `ACTION_OUTPUT_BINDING_MISMATCH`，owner=`action_generation`，route=Generate；repair payload 无 hash，只要求使用 semantic handle；
- Plan 把 required output 重绑定为 `bb.fa`：owner=`plan_binding`，route=Planner Repair；
- Generate handle 正确但 Controller/Renderer 物化为 `bb.fa`：owner=`controller_projection`，不得路由模型盲修；
- materialized action 指向 `aa.fa` 但工具实际产生 `bb.fa`：owner=`observed_artifact_mismatch`，route=Recovery；
- unknown、duplicate、wrong-direction placeholder 均在执行前失败；
- managed path 的 argv/env materialization 正确处理空格、引号和 shell metacharacters，不进行不安全文本替换；
- legacy exact literal path compatibility 命中被记录，新 session 禁止生成 literal required output path；
- relocation 六项条件逐项测试；旧文件、其他 attempt 文件、多候选文件或内容不合格时不得 `mv` 后完成。

### 18.5 原子性与恢复

- 在 object write、tmp write、fsync、replace 前后注入故障；
- 恢复只能读取 N 或 N+1 完整 generation；
- runtime-only generation N+1 可继续引用 generation N 创建的 ApprovalSnapshot，且不会重新物化或修改 snapshot；
- 进程内并发和多进程并发 proposal 都只有一个 CAS 成功；
- generation re-check 和 replace 位于同一 exclusive writer critical section；
- state ref 不得指向未落盘 object；
- audit event 的 `previous_event_ref`、generation before/after、event count 和 `audit_head` 连续；
- 长 session 的 current state audit index 保持 bounded，不随 event 数线性保存 ref 数组；
- event chain 断裂产生 corruption diagnostic；
- crash 后 orphan immutable object 可以垃圾回收，但不能污染 current state；
- migration 多次运行结果一致。

### 18.6 Recovery 路由

- runtime command failure -> Generate；
- plan dependency/binding error -> Planner Repair；
- source-bound user ambiguity -> Consultant/user；
- approval stale -> Review/Approval；
- ref/hash/generation mismatch -> internal protocol block；
- 没有专用 skill 但通用工具足够 -> Planner 可继续；
- 真正能力缺失 -> `CAPABILITY_UNAVAILABLE`；
- Recovery 无语义列表尾部截断。

### 18.7 Completion

- 文件预存在但非当前 attempt 生成不能完成；
- action PASS、step gate FAIL 的 scope 区分正确；
- 所有 output contracts 只从 canonical 真源读取；
- Approval Gate applicability 非 `applicable` 时失败，但 provenance-only record hash 差异不单独导致失败；
- required steps/outputs/evidence 全部有效时完成。

### 18.8 Incident replay

至少回放：

- `session_1786335039`：验证 TaskSpec 去重、节点视图完整性和 Recovery 不截断关键语义；
- 既有 snpEff plan/renderer convergence fixtures：验证 custom database、`genome_version`、output ownership 和 approval gate；
- 旧 schema session：验证幂等迁移、旧 approval 重新验证和 ref 缺失诊断。
- managed artifact `aa.fa/bb.fa` fixture：验证 plan/action/controller/observed 四个 mismatch owner、placeholder materialization 和 relocation restrictions。

schema 迁移的等价性比较应基于 canonical semantic contract、Plan contract、approval scope 和最终行为，不要求新旧 JSON 字节完全相同。

## 19. 验收指标

### 19.1 必须通过的正确性指标

- exactly one writable current TaskSpec；
- exactly one writable current PlanSpec；
- exactly one durable state writer；
- exactly one graph transition owner；
- 每次 LLM invocation 都有 ControllerBinding 可追溯到 source semantic/integrity state，但这些 control identities 不出现在 LLM prompt/response；
- LLMPayload 只包含 semantic facts/handles/contracts/evidence/violations/allowed operations；
- unrelated audit/provenance/telemetry changes 不触发 Planner/Generate/Recovery 的无效重调；
- 所有批准都固定 task+plan semantic identities 和批准时 record identities；
- ApprovalSnapshot 与 derived ApprovalGate applicability 严格分离；
- PlanValidationState 与 PlanSpec semantic payload 严格分离；
- Renderer/Skill execution contracts 使用版本化 hash identity；
- required managed artifact paths 由 Controller/Renderer materialize，LLM 只引用 semantic handles；
- output mismatch 在 plan/action/controller/observed 四个 owner 边界准确路由；
- current audit index bounded 且 audit event chain 可验证；
- 无 required semantics 因 prompt budget 消失；
- 无节点通过 compatibility alias 写回权威状态；
- old session migration 幂等且不伪造批准/证据；
- CompletionGate 不依赖 legacy duplicate fields。

### 19.2 体积与效率指标（非语义硬门槛）

以下是期望趋势，不得通过裁剪语义强行满足：

- 新 session 中 `task_state.task_spec` 精确副本为 0；
- duplicate outputs/facts aliases 新写入为 0；
- inline `consultation_state.final_contract` 新写入为 0；
- 原始请求正文只持久化一次；
- Planner/Generate/Recovery prompt 中同一 constraint 正文只出现一次；
- LLM prompt/response 中实际 control-plane field/value（hash value、generation value、CAS/ref value）出现次数为 0（显式诊断任务除外）；
- `session_1786335039` 等价 fixture 的 durable current state 和节点 prompt 显著小于基线；
- payload 构建报告 top semantic contributors；trace 开启时可附 projection fingerprints，关闭时不影响业务。

不设 8k/10k/12k 的硬 TaskSpec 字符目标。若一个真实任务本身需要大量有效约束，允许 TaskSpec 合理增长；系统应通过精确闭包、refs 和模型容量选择管理上下文，而不是丢失语义。

## 20. 兼容、回滚和发布策略

### 20.1 Feature flags

建议使用：

- `STATE_CONTRACT_V2_WRITE`
- `STATE_CONTRACT_V2_READ`
- `TASK_VIEW_V2_SHADOW`
- `TASK_VIEW_V2_ENFORCE`
- `APPROVAL_SNAPSHOT_V2_ENFORCE`
- `NODE_CONTEXT_V2_SHADOW`
- `LLM_PAYLOAD_V2_ENFORCE`
- `MANAGED_ARTIFACT_V2_ENFORCE`

flags 必须有明确退役日期，不能成为永久双状态架构。

### 20.2 双读/影子比较

过渡期同时计算旧投影和新 LLMPayload，但只有一边驱动行为。比较：

- canonical constraints coverage；
- inputs/outputs；
- current step bindings；
- LLMPayload control-metadata leakage；
- semantic handle coverage/resolution；
- approval identity；
- Recovery failure owner；
- output mismatch owner 与 materialized path；
- prompt size/top contributors。

任何 mismatch 必须按 field/ID 记录，不仅记录 hash 不同。

### 20.3 回滚

回滚只能切换 read/enforce path，不得删除已经写入的 v2 audit/object。旧 reader 若无法安全解释 v2 state，应显式拒绝启动，而不是降级为缺失 TaskSpec。

已创建的 ApprovalSnapshot 不能在回滚时转换为 latest aliases。执行过的 provenance/evidence 必须保留。

## 21. 实施前必须冻结的设计决定

本规范给出以下默认决定；若实现者不同意，必须在代码修改前单独提出 RFC，而不能在实现中隐式改变：

1. **优先级**：权威边界和行为等价优先于 prompt 体积。
2. **物理存储**：Phase 1 保持单个 `plan_state.json` 原子边界。
3. **constraint identity**：先保留当前完整稳定 ID，不立即换短 ID。
4. **hash**：Task/Plan 分离 semantic hash 与 record hash；两种 hash 都使用版本化 canonical serializer，且绑定内容而非文件路径。
5. **audit**：append-only、`previous_event_ref` 链式连接；current state 只保存 bounded head/count/latest index，Audit 不是正常执行隐藏依赖。
6. **node context**：分离 ControllerBinding、optional ProjectionTrace 和 LLMPayload；只有 LLMPayload 进入模型，trace fingerprints 是可选观测数据。
7. **approval**：ApprovalSnapshot 只保存不可变历史 decision；当前 applicability 由 gate 动态计算，semantic 变化默认使旧批准不适用。
8. **capability**：专用 skill 缺失不等于能力缺失。
9. **migration**：先双读核对 consumer，再停止旧字段写入，最后删除。
10. **错误处理**：内部 state/ref/schema 问题绝不伪装成用户歧义。
11. **generation**：current commit generation 与 immutable object `created_generation` 分离；旧 snapshot/object 可被后续一致状态引用。
12. **CAS**：generation re-check 与 atomic replace 必须位于真实 exclusive writer critical section。
13. **validation**：PlanValidationState 不进入 PlanSpec semantic payload 或 hash。
14. **registry identity**：Plan execution identity 固定 Renderer 与 execution-semantic Skill contract version/hash。
15. **LLM boundary**：cryptographic identities、generation、CAS tokens 和 refs 不进入正常 LLM prompt/response；Controller 自动 bind/stamp。
16. **staleness**：按节点 relevant semantic/execution domain 判断；global generation 不是通用失效开关。
17. **managed artifact**：required artifact authoritative path 由 Controller/Renderer 物化，LLM 只用 semantic handles。

## 22. 完成定义

当且仅当以下条件全部满足，本重构才算完成：

1. 新 session 只存在一份可写 canonical TaskSpec；
2. TaskSpec、PlanSpec、PlanValidationState、ApprovalSnapshot、ApprovalGate result、RuntimeState、Audit 生命周期在 schema 和代码 owner 上均分离；
3. TaskSpec/PlanSpec 均分别维护正确 hash domain 的 semantic/record identities；
4. Consultant 只返回 semantic mutation；ControllerBinding/commit 层自动验证并 stamp generation 与 identities；
5. Planner/Repair 只返回 semantic plan proposal/delta；Controller 自动 bind/stamp；
6. Consultant、Planner、Generate、Recovery、Review 使用统一 `ControllerBinding + optional ProjectionTrace + LLMPayload` framework；
7. 正常 LLM prompt/response 不含 hashes、generation、approval/content refs、CAS token 或 trace fingerprint；
8. 每个 LLMPayload 的 semantic handles 可唯一解析，选择闭包可由控制面证明；trace 关闭不影响运行；
9. Node-specific staleness tests 证明无关 metadata 变化不会制造重复 LLM 调用；
10. Managed required artifacts 使用 handles/placeholders，authoritative paths 由 Controller/Renderer 确定性物化；
11. `aa.fa/bb.fa` 四类 mismatch 在执行前或正确 owner 边界被识别，relocation 不得伪造完成；
12. Recovery 不再通过 generic tail truncation 丢失约束；
13. ApprovalSnapshot 永不因 stale 被改写，Approval Gate 动态校验 task/plan semantic identities 和 applicability；
14. Plan validation metadata 不改变 PlanSpec identity；
15. Plan 固定 Renderer/Skill execution contract identities；
16. Audit chain 可验证且 current index bounded；
17. CompletionGate 只依赖 canonical contracts 和 current-run evidence；
18. PlanTracker 和 Controller 的唯一写入/路由 ownership 未被破坏，跨进程 CAS 有真实临界区；
19. `session_1786335039` 与 snpEff/managed-artifact fixtures 回放通过；
20. 旧 session 可幂等迁移且旧批准不会被误继承；
21. consumer inventory 证明待删除 aliases 已无 authoritative reader；
22. 新写入中已消除确认的精确副本；
23. 文档、schema、指标、故障注入和回滚测试齐全。

在上述条件完成前，不应把“Generate 的 max tokens 已提高”或“某个 prompt 暂时变短”视为问题已经解决。
