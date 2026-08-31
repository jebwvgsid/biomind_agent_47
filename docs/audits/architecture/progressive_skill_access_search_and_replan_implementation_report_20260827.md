# BioMind 渐进式 Skill 读取、搜索、绑定与计划重规划实施报告

日期：2026-08-27

规范状态：`IMPLEMENTATION SPEC`

实现状态：已完成生产实现、离线回归与差异审计；未运行真实工作流

## 1. 结论

本轮已在现有 BioMind Controller、Planner、Consultant、Generate、Recovery 和
PlanTracker 链路上实现渐进式 Skill Access，没有建立第二套 Planner、Consultant
或 Skill Discovery，也没有让 Controller 解释 snpEff、ANNOVAR 或其他领域语义。

最终行为满足以下边界：

- canonical `selected_skill_refs` 指向 session-local immutable snapshot；下游按需取得
  exact excerpt，而不是只接收 capabilities 摘要。
- `REQUEST_SKILL_CONTEXT`、`SEARCH_SKILLS` 和 `PROPOSE_SKILL_BINDING` 是 typed
  node decision，不是 shell action，也不经过 Execute。
- Skill 读取/搜索完成后通过 continuation 返回原 caller；不增加执行失败或重试计数。
- 搜索只创建 candidate snapshot，不自动激活、不改 TaskSpec、不改 Plan。
- step supplement、task-level supplement 和 replacement 分别经过受限 Consultant
  review、主 Consultant TaskSpec commit 和 Planner plan-impact review。
- TaskSpec、Plan、user-turn、step、continuation 和 action identity 都受 revision/CAS
  gate 保护；旧节点或旧执行结果只产生 typed stale，不会进入 Execute。
- catalog 不完整、无匹配、无进展、路径越界和 hash 漂移均有不同的结构化分类。

任务专属离线回归全部通过。扩展兼容性回归保留 5 个已在任务起点基线复现或定位为
任务外代码的既有失败；本轮没有为追求“全绿”扩大修复范围。

## 2. 工作区安全记录

### 2.1 快照

| 项目 | 结果 |
| --- | --- |
| 仓库 | `/public/home/liusiqi/03.毕设/Biomind_v1` |
| HEAD | `66eb34a59f9823469b36d13cd0007b104b34769d` |
| 分支 | `codex/save-current-work` |
| 暂存区 | 空；最终检查 `git diff --cached --quiet` 返回 0 |
| 工作区 | 仍为 dirty；最终报告生成前 `git status --short` 共 142 行 |
| Git commit | 未创建 |

工作区在任务开始时已含大量修改。实现期间没有执行 `git add`、`git commit`、
`git reset`、`git clean`、`git checkout -- .` 或 `git restore .`，也没有清理、覆盖或
回滚用户已有修改。

### 2.2 禁止项遵守情况

- 未读取或运行 `/public/home/liusiqi/03.毕设/启动.sh`。
- 未读取或打印 API Key。
- 未运行真实 BioMind、snpEff 或 ANNOVAR 任务。
- 未调用真实模型/API；测试只使用 deterministic fake/mock。
- 未修改生产 Skill 文档，未下载、创建或自动激活任何生产 Skill。
- 未重写 `skill_discovery.py`，未修改其排序/检索算法或存储方式。

## 3. PREEXISTING_DELTA 与 THIS_TASK_DELTA

### 3.1 归因方法

任务开始时为关键文件保存了独立字节基线：

`/tmp/biomind-progressive-skill-baseline.qYlgOp/`

因此下列统计是相对“本任务开始时工作区内容”，不是相对 HEAD；这避免把原有 dirty
worktree 误计为本任务修改。

| 文件 | 本任务开始基线后的净增/删 |
| --- | ---: |
| `agent_core/node_context.py` | `+143 / -12` |
| `agent_core/skill_manager.py` | `+181 / -62` |
| `agent_core/plan_tracker.py` | `+67 / -1` |
| `agent_core/state.py` | `+17 / -1` |
| `bio_agents.py` | `+4781 / -208` |
| `prompts/manager.py` | `+399 / -25` |

以下文件与任务开始基线逐字相同，证明没有重写既有发现、路由或 TaskSpec 子系统：

- `agent_core/skill_discovery.py`
- `agent_core/consultation_state.py`
- `agent_core/router.py`
- `agent_core/task_spec.py`

### 3.2 明确属于 THIS_TASK_DELTA

新建生产边界模块：

- `agent_core/skill_access.py`（2959 行）

新建离线回归：

- `tests/test_progressive_skill_access.py`（1487 行）
- `tests/test_progressive_skill_stale_gates.py`（287 行）
- `tests/test_progressive_skill_acceptance_scenarios.py`（783 行）
- `tests/test_recovery_progressive_skill_access.py`（463 行）
- 本实施报告

在上述基线文件中的净变化，以及 `agent_core/middleware.py`、`config/config.py`、
`tests/test_attempt_recovery_diagnosis_contract.py` 和
`tests/test_consultant_first_canonical_state.py` 的本任务局部 hunks，也属于
`THIS_TASK_DELTA`。

### 3.3 明确保留为 PREEXISTING_DELTA

当前全仓 `git diff --stat` 覆盖 63 个 tracked files，且 `git status --short` 包含大量
本任务外路径。这些全局差异不能归因于本任务。特别是 `agent_core/middleware.py`、
`config/config.py` 和两个既有测试文件在任务开始前已经 dirty，未保存完整独立基线，
因此本报告只对实际添加的渐进式 Skill hunks 负责，不声称这些文件的整个 HEAD diff
属于本任务。

## 4. 修改文件与职责

| 文件 | 本轮职责变化 |
| --- | --- |
| `agent_core/skill_access.py` | 唯一新增生产边界；typed decision 解析、immutable snapshot、heading/excerpt/resource、search cache、continuation、binding proposal/review、TaskSpec Skill 事务、PlanImpact 校验/应用、事件与 stale result |
| `agent_core/skill_manager.py` | catalog completeness/revision；安全枚举与 legacy reference/script resolver 的 root/skill-dir containment |
| `agent_core/node_context.py` | 将 active refs、heading index、exact excerpt refs/content 和 tool result 做有界投影；按节点声明相关 stale preconditions |
| `agent_core/plan_tracker.py` | 将 Skill transaction 字段纳入 durable plan state，并使用 generation/CAS precondition 写入 |
| `agent_core/state.py` | 声明 canonical Skill、continuation、proposal、PlanImpact、stale 和 user-turn revision 字段 |
| `agent_core/middleware.py` | Recovery continuation 与 stale result 路由，不把 Skill 获取计为 execution failure |
| `bio_agents.py` | 将服务接入 Consultant/Planner/Generate/Corrective Generate/Recovery；restricted review、atomic TaskSpec commit、PlanImpact apply、same-node resume、CAS、Execute identity gate |
| `prompts/manager.py` | 发布三个 node decision schema、restricted Consultant schema、PlanImpact schema及 active-only/search-not-activate 规则 |
| `config/config.py` | 六个命名预算/大小限制，避免散落 magic number |
| `tests/test_progressive_skill_access.py` | snapshot、exact read、search、proposal/review、PlanImpact、路径、缓存/no-progress 和领域无关回归 |
| `tests/test_progressive_skill_stale_gates.py` | node/step/action revision 与 durable action identity stale gate |
| `tests/test_progressive_skill_acceptance_scenarios.py` | 规范 23 章和场景 A/B 的离线组合验收 |
| `tests/test_recovery_progressive_skill_access.py` | Recovery same-node continuation、post-review context request、user-turn stale |
| `tests/test_attempt_recovery_diagnosis_contract.py` | 既有 Recovery/attempt identity 契约与新 action identity gate 联合回归 |
| `tests/test_consultant_first_canonical_state.py` | Skill transaction 与 TaskSpec candidate 原子提交/CAS 回归 |

## 5. 新增协议

### 5.1 Node decision

```text
REQUEST_SKILL_CONTEXT
  reason
  requests[]: skill_id + operation + operation-specific fields

SEARCH_SKILLS
  query
  capability_gap
  desired_outcome
  requested_scope

PROPOSE_SKILL_BINDING
  candidate_skill_id / candidate_snapshot_id
  relation: supplement | replace
  requested_scope: step | task
  justification / evidence_refs
```

`REQUEST_SKILL_CONTEXT` 支持 `headings`、`section`、`search`、`range`、`resource`
和 `full`。过长全文返回 `REQUIRES_NARROWING` 与 heading index，不做静默截断。

Harness 自动绑定且 LLM 不得生成的 envelope 为：

```text
caller_node
caller_invocation_id
continuation_id
task_revision
plan_revision
authoritative_user_turn_revision
current_step_id
attempt_id
```

### 5.2 受限 Consultant review

```text
APPROVE_STEP_SUPPLEMENT
ROUTE_TASK_LEVEL_CHANGE
REJECT_SKILL_PROPOSAL
NEED_USER_INPUT
SEARCH_MORE
```

此模式只审候选适用性、supplement/replace、step/task scope、意图变化和真正的用户
输入缺口；禁止 bootstrap、普通 FINISH、Plan 和 shell。task-level 或 replacement
必须回主 Consultant，以一笔 TaskSpec transaction 精确提交最终 Skill 集合。

### 5.3 Planner PlanImpact review

```text
BIND_AND_RESUME
PATCH_CURRENT_STEP
REPLAN_SUFFIX
FULL_REPLAN
NEED_USER_INPUT
REJECT_PROPOSAL
```

每个 decision 必须携带 `base_task_revision`、`new_task_revision`、
`base_plan_revision`、`reason` 和类型专属字段。`BIND_AND_RESUME` 只允许当前 step
supplement；replacement 不能借此绕过重规划。所有 `skill_refs` 在 apply 前后都必须与
canonical active refs、scope、target、snapshot 和 `skill_ids` 精确一致。

## 6. 新增状态

### 6.1 AgentState 第一等字段

```text
selected_skill_refs
selected_skill_ids                 # compatibility projection only
skill_access_state
skill_continuation_id
skill_decision_result
pending_skill_binding_proposal
pending_plan_impact_review
plan_impact_review
plan_impact_review_error
stale_node_result
stale_execution_result
execution_result
authoritative_user_turn_revision
```

### 6.2 `skill_access_state` durable 内容

- immutable skill/resource snapshots 与 heading index；snapshot identity 绑定
  `skill_id + content_hash + catalog_revision`。
- canonical `selected_skill_refs`，status 闭集仅为 `active|superseded|candidate`；
  review disposition 单独存放，不污染 status vocabulary。
- exact excerpts、excerpt cache、search cache、continuations、search records、binding
  proposals、tool results、调用计数与结构化 events。
- normal node projection 只暴露 active refs；candidate evidence 仅在明确 restricted
  review projection 中可见。

## 7. 路由前后对比

### 7.1 修改前

```text
Consultant 读取 Skill
  -> selected_skill_ids / capability projection
  -> Planner / Generate 主要看到摘要
  -> 节点缺少原文时只能猜测，或在 Execute/Recovery 后补救
```

运行中没有一个统一、typed、可续接且带 revision 的 Skill context/search/binding
事务；搜索结果、任务语义变更与计划变更也没有被分阶段隔离。

### 7.2 修改后：读取

```text
Generate | Planner | Recovery | Consultant
  -> REQUEST_SKILL_CONTEXT
  -> SkillAccessService 从 immutable snapshot 返回 exact excerpt
  -> durable continuation + typed result
  -> 同一 caller 重新进入
```

该链路不经过 Execute、用户审批、Recovery、CompletionGate 或 step retry。

### 7.3 修改后：搜索与绑定

```text
caller -> SEARCH_SKILLS
       -> 复用现有 Skill Discovery
       -> candidate snapshots + typed completeness
       -> 同一 caller continuation
       -> PROPOSE_SKILL_BINDING
       -> restricted Consultant review
          -> step supplement: Planner PlanImpact
          -> task/replace: main Consultant atomic TaskSpec commit
                         -> Planner PlanImpact
```

search、candidate read、proposal、review、activation 和 plan change 是六个不同阶段。
唯一候选也不会自动激活。

### 7.4 修改后：stale

```text
LLM/tool result returns
  -> re-read durable authority
  -> compare task/plan/user/step/continuation/action identity
  -> mismatch: STALE_NODE_RESULT or stale execution result
  -> discard; no Execute, no retry/failure increment
```

step-scoped PlanImpact 若 current step 已变化，直接 typed stale 并关闭旧 continuation，
不会静默 rebase 或形成 review loop。

## 8. TaskSpec、Plan revision 与 CAS

- 主 Consultant 对 task-level Skill supplement/replacement 精确校验“旧集合 -> 新集合”，
  并以单次 transaction 同时提交 TaskSpec、semantic hash、canonical Skill refs 和
  `task_revision + 1`；不能额外删除或增加第三个 Skill。
- bootstrap 后任何 TaskSpec semantic/method change，即使 Skill IDs 未变化，也会创建
  `pending_plan_impact_review`，记录 old/new TaskSpec、hash 和 revision。
- Planner restricted input 固定 `base_task_revision`、`new_task_revision`、
  `base_plan_revision`、user-turn、step 和 proposal/transaction identity；LLM 返回后再次
  校验。
- PlanImpact 应用成功只增加一次 Plan revision。`REPLAN_SUFFIX` 保留已完成前缀并要求
  具体 evidence refs；旧 suffix 标记 superseded 而非物理删除，新 suffix 使用新 ID。
- 所有 Skill durable prepare/continuation 路径使用 state-generation CAS 或安全 CAS retry；
  proposal pointer、transaction hash、snapshot/excerpt linkage 在调用前后都复验。
- Corrective Generate cache key 包含 task/plan/user/step/attempt/action identity，缓存命中
  和新结果都重新校验。
- Execute 在真正运行前及每次 commit 前都要求 durable pending `action_id` 与
  `step_id`、task/plan/user revisions、`plan_id` 和 execution epoch 一致；attempt
  lineage 继续由既有 attempt/Recovery contract 单独校验。旧 action 被替换或删除即
  stale。

## 9. 安全、完整性与预算

- Skill source 必须是注册/发现结果，resolve 后位于 configured skills root 内。
- resource、reference 和 script loader 只接受当前 Skill 目录下相对路径；拒绝绝对路径、
  `..` 和逃逸 symlink。
- snapshot 保存 immutable bytes，不会因 live `SKILL.md` 修改而漂移；读取时校验 content
  hash、snapshot ID 和 excerpt/resource linkage。
- catalog provider/entry 错误返回 `SKILL_CATALOG_INCOMPLETE`/`SEARCH_ERROR`，不会降级
  为 `NO_MATCH`、`USER_INPUT_MISSING` 或 `TASK_BLOCKED`。
- read/search cache key 含 snapshot/catalog 与 authority revision；连续无新信息返回
  `SKILL_CONTEXT_NO_PROGRESS` 或 `SKILL_SEARCH_NO_PROGRESS`。
- 命名配置为 `SKILL_FULL_READ_MAX_CHARS`、`SKILL_EXCERPT_MAX_CHARS`、
  `SKILL_CONTEXT_MAX_REQUESTS_PER_ROUND`、
  `MAX_SKILL_CONTEXT_ROUNDS_PER_INVOCATION`、
  `MAX_SKILL_SEARCH_ROUNDS_PER_STEP` 和
  `MAX_SKILL_BINDING_PROPOSALS_PER_STEP`。

## 10. 结构化事件

已实现：

```text
skill_snapshot_created
skill_context_requested
skill_context_served
skill_search_requested
skill_search_completed
skill_binding_proposed
skill_binding_reviewed
plan_impact_reviewed
node_continuation_resumed
stale_node_result_discarded
```

事件只保存来源、decision、revision 和引用，不展示模型内部推理。

## 11. 必须回归测试覆盖矩阵

| 规范 | 覆盖 | 结果 |
| --- | --- | --- |
| 23.1 snpEff 首次生成先读原文 | `test_snpeff_first_generate_reads_exact_rules_before_action`；fake Generate 第一次 request、第二次依据 exact fixture 生成 action | PASS |
| 23.2 读取不算失败 | `test_context_request_uses_cache_then_returns_no_progress_without_failure_counts` 与场景测试 | PASS |
| 23.3 Corrective Generate 继承 excerpt | progressive access + Recovery context projection/continuation tests | PASS |
| 23.4 搜索返回当前节点 | `test_generate_search_returns_to_same_node_without_activation` | PASS |
| 23.5 候选不自动激活 | search/candidate status、active-ref non-downgrade tests | PASS |
| 23.6 多候选 restricted Consultant | typed `review_route=consultant_skill_resolution`、candidate-only projection 和 restricted prompt/schema tests | PASS |
| 23.7 明确 snpEff -> ANNOVAR | `test_explicit_snpeff_to_annovar_switch_replans_and_stales_old_generate` | PASS |
| 23.8 模糊 ANNOVAR | `test_ambiguous_annovar_mention_asks_once_without_replacing_skill` | PASS |
| 23.9 suffix replan | `test_replan_suffix_carries_passed_prefix_and_retains_superseded_history` | PASS |
| 23.10 incomplete catalog | `test_search_distinguishes_matches_no_match_and_incomplete_catalog` 等 | PASS |
| 23.11 重复搜索保护 | cache/no-progress 与 shared step budget tests | PASS |
| 23.12 任意路径读取阻断 | absolute/parent/outside symlink + legacy loader tests | PASS |
| 23.13 Controller 领域无关 | 两类 fixture 与 production-added-line 关键词审计 | PASS |

场景 A 通过 deterministic two-pass Generate 组合测试验证：第一次 action 产生前 exact
Skill 原文已读取；没有先进入 Execute、Recovery 或 ask-user。场景 B 通过离线
TaskSpec transaction + PlanImpact + stale gate 组合测试验证：方法切换增加 Task revision、
旧 Skill superseded、新 Skill active、Plan 后缀重规划、旧 Generate 不执行。

23.1 与场景 B 按规范使用 fake provider/pure service composition，不调用真实 LLM、真实
graph workflow 或真实领域工具。领域词和预期 flags 只存在于测试 fixture/mock action，
不在通用生产控制器中。

## 12. 实际运行的验证

所有命令都在 BioMind Python 环境中预加载 `langchain_core.messages`、
`langchain_core.callbacks` 和 `pytest`，以避开仓库既有 `conftest` 导入冲突；没有安装
依赖。

### 12.1 任务专属核心套件

```text
tests/test_progressive_skill_access.py
tests/test_progressive_skill_stale_gates.py
tests/test_progressive_skill_acceptance_scenarios.py
tests/test_recovery_progressive_skill_access.py

45 passed, 8 subtests passed
```

### 12.2 任务相关扩展套件（最终代码）

```text
上述四个文件
tests/test_attempt_recovery_diagnosis_contract.py
tests/test_consultant_first_canonical_state.py

75 passed, 14 subtests passed, 1 failed
```

唯一失败是既有 `test_10_equivalent_retry_without_progress_is_blocked`，详见第 13 节。

### 12.3 SkillManager / Prompt / Plan 兼容性套件（最终代码）

```text
tests/test_skill_manager.py
tests/test_prompt_manager.py
tests/test_plan_lifecycle.py

138 passed, 3 subtests passed, 4 failed
```

其中 `tests/test_skill_manager.py` 单独运行结果为 `58 passed`；四个失败均为任务起点已
存在或位于本任务未修改逻辑的既有失败，详见第 13 节。

### 12.4 静态检查

- 生产模块和新增测试的 Python syntax compile：PASS。
- 本任务文件 `git diff --check`：PASS。
- 相对任务开始基线与新生产模块的 added-line 审计未发现
  `snpeff|annovar|nocheckcds|nocheckprotein`（大小写不敏感）：PASS。
- final read-only code audit：未发现剩余 P0/P1；重点复核 TaskSpec semantic change、
  step-scoped stale、路径 containment、CAS、exact Skill set、PlanImpact refs 与 Execute
  action identity。

## 13. PREEXISTING_FAILURE

| 测试 | 失败事实 | 归因 |
| --- | --- | --- |
| `test_consultant_first_canonical_state.py::...test_10_equivalent_retry_without_progress_is_blocked` | `consultant_no_progress_decision` 返回 allow=true | `agent_core/consultation_state.py` 与任务开始基线逐字相同；非本任务引入 |
| `test_prompt_manager.py::...test_consultant_prompt_is_compact_and_domain_neutral` | 既有 Consultant prompt 长度 7253 > 6500 | 使用任务开始基线 `prompts/manager.py` 复现 |
| `test_prompt_manager.py::...test_detailed_planner_prompt_is_centralized` | 既有测试仍期待已移除的 `semantic_constraint_ids` | 使用任务开始基线 `prompts/manager.py` 复现 |
| `test_prompt_manager.py::...test_recovery_prompts_separate_judgment_from_repair_generation` | 测试期待 schema v2，当前既有 prompt 是 v4 | 使用任务开始基线 `prompts/manager.py` 复现 |
| `test_plan_lifecycle.py::...test_manifest_absent_renderer_step_rejects_step_completion_candidate` | 实际 guard 为 `step_completion_candidate_rejected`，测试期待更旧的 renderer-specific 文本 | 对应 transition-guard 分支不在本任务基线差异内 |

这些失败均未影响渐进式 Skill 测试。按规范“不要顺手处理”原则，本轮未修改相应既有
契约或测试期待。

## 14. 未运行的测试与原因

- 未运行全仓完整 pytest：仓库规模大、工作区已有大量无关修改，且本规范要求局部离线
  单元/集成验证；已运行所有直接相关模块及邻接兼容性套件。
- 未运行完整真实 BioMind graph/workflow：规范明确禁止。
- 未运行 `/public/home/liusiqi/03.毕设/启动.sh`，也未读取其内容：规范明确禁止。
- 未调用任何真实 LLM/API：避免密钥和网络边界，规范要求 fake provider。
- 未运行真实 snpEff/ANNOVAR：规范明确禁止大任务；事故和切换用 fixtures 验证。
- 未测试远程 Skill 市场、自动下载或自动生成 Skill：均为 non-goal。

## 15. 与规范的偏差

生产架构没有已知偏差。实现只新增一个生产边界模块 `agent_core/skill_access.py`，复用
现有 Skill Discovery、Planner、Consultant、PlanTracker 和 state persistence。

验证层面保持规范要求的离线粒度：23.1 使用 deterministic fake second-Generate；多候选
审查以 restricted schema/prompt/projection 和路由组合测试验证；场景 B 以 service、
TaskSpec transaction、PlanImpact 和 stale gate 的离线组合测试验证，而非运行被规范禁止
的完整真实工作流。这是测试执行边界，不是生产行为降级。

## 16. 完成标准复核

规范第 26 节的 15 项均已由实现和上述回归覆盖：immutable snapshot、原文按需读取、
same-node continuation、不经 Execute/Recovery、任意目标节点本地搜索、search-not-activate、
Consultant task/replace review、Planner PlanImpact、TaskSpec revision、stale gate、领域无关
Controller、snpEff 首次生成事故、snpEff -> ANNOVAR 切换、catalog completeness 和
read/search no-progress/budget 均为 PASS。

本轮未创建 Git commit；工作区保留用户全部既有修改。
