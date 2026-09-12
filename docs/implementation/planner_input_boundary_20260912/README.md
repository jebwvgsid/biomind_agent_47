# Planner 协议、候选生命周期与输入边界实施回执

本轮实现 PF-1、LIFE-1、INPUT-1，并将 MSG-1 并入相关代码。未运行真实模型/API/WGS，未恢复原 session，未读取原业务数据库、启动脚本或敏感配置。未 push。

## 基线、提交与范围

实际分支 `refactor/consultant-controller-authority`；起点 `51834d36531b641efb1c70ac3ad756a64b9a1d09`。起点索引为空，437 条已有工作区状态均保留。`prompts/manager.py` 原有 HASH_OUTPUT_POLICY 等 19 增/1 删通过独立 index 排除；其余既有源码、测试、删除及未跟踪内容未暂存。

| 切片 | 完整 commit | 文件范围 |
|---|---|---|
| PF-1 + Generate 公开说明 | `c9ffdce614e8bd6366a396404275ffd744d1e743` | `agent_core/plan_contract.py`、`prompts/manager.py`、`tests/export_planner_boundary_fixture.py`、`tests/test_planner_boundary.py` |
| LIFE-1 + 批准阶段提示 | `f52b15f3489c4c7bdb318866c9db9a246cfae051` | `agent_core/plan_tracker.py`、`agent_core/planner_loop.py`、`agent_core/workflow/early_nodes.py`、`bio_agents.py`、两个 `test_planner*boundary.py` |
| 同候选重复提交幂等 | `025922d856c72e86f9cd9a6f2ac7e8b92248a442` | `agent_core/planner_loop.py`、`tests/test_planner_lifecycle_boundary.py` |
| INPUT-1 主切片 | `61e751b4114fa1ebe203c8ff2ccf8194dc057f0e` | `agent_core/cli_plain.py`、`agent_core/human_io.py`、`bio_agents.py`、`tests/replay_input_boundary_pty.py`、`tests/test_input_boundary.py`、`tests/test_approval_owner_lifecycle.py` |
| 空白粘贴不能批准 | `0a30cc3311c185b05383d49c5d3673c88b4ee7b1` | `agent_core/cli_plain.py`、`agent_core/cli_terminal.py`、`tests/test_input_boundary.py`、`tests/replay_input_boundary_pty.py` |

测试导出来自精确 Git tree，不包含原 dirty 改动。最终生产/测试源码对应最后一个上述提交。文档另行提交，其 parent 即该源码版本；不把用户整个 dirty 工作树称为已验收树。

## 协议与状态来源

| 来源/函数 | 原问题 | 当前行为与证据 |
|---|---|---|
| `plan_contract.PLAN_RESULT_PATH_POLICIES` / `structured_plan_candidate_diagnostics` | reader 只接受两个新值，说明仍列旧值 | validator expected 和 Planner 允许值片段共用已有常量；不添加旧值别名 |
| `PromptManager.get_planner_v2_system_prompt` | 可达正文 `For path_policy=planner_materialized` | 移除冲突指令，区分现行两种策略及 legacy TaskSpec 命名空间；保留 Consultant 的合法旧术语 |
| `managed_artifacts` 的 binding / `reconcile_runtime_outputs` | 路径所有权与结果接纳不能混为一谈 | `managed_exact` 保留精确路径约束；`model_selectable` 保留现有可定位实际结果的语义；消费代码未改，异名结果回归仍使用原流程 |
| Planner `persist_planner_rejection` → correction context | 六处错误本来已有完整机器反馈 | 保留 JSONPath、observed、expected、原候选与完整原响应；一次现有纠正，不增加 Consultant/Recovery/旁白调用 |
| `record_planning_failure` | 返回失败但保留的 loop 仍像旧成功 | producer 写本轮 `candidate_rejected` 生命周期，保留旧 Plan/receipt；新合法候选通过原 resolve 流程收口 |
| `planner_loop.plan_review_candidate_scope` / `_receipt_matches_plan` | 只对 Plan 身份，不对当前任务/调用 | 共用既有 task revision、user turn revision、epoch、claim、candidate scope，检查当前成功结果；新调用、失败、取消或旧输入不能借旧 receipt |
| `_TypedEarlyNode.run` / `TypedPlanner` | durable merge 保留历史，consumer 把历史当本轮结果 | 不返回全量旧 state；保留原 reducer，改为消费 producer 写出的 scoped lifecycle |
| `plan_review` / `record_plan_review_acceptance` | 审批后直接使用旧对象 | 展示前使用同一 readiness；提交前重新加载并核对展示 submission，CAS 失败不批准；批准事实成功保存后才显示 Preparing |
| `_commit_plan_review_submission` | 延迟/重复提交可能重新选旧对象 | 核对当前 claim 和 Plan identity；同一候选重投递复用原 submission/acceptance |
| `_resolve_schema2_resume_state` / `TypedPlanReview` | checkpoint 的旧待审标志可绕过当前资格 | 同一 readiness；过期 review 回到当前 Planner，不显示旧审批 |
| Execute 的 `_stale_plan_task_revision_reason` / preflight | 最后保护此前成功拦截旧 action | 未移除、未放宽；强送合成旧 action 仍 `stale_result`，执行启动 0 |
| Generate 正式 prompt / PlanReview 系统提示 | 候选文字可能被理解为检查完成 | 明确未运行候选只能描述将做的检查；原公开模型文字保留，不加语义校对模型；批准提示改为准备当前步骤 |

`evidence/pf1_provider_messages.json`、`life1_provider_messages.json` 保存实际 fake provider messages。验证器区分当前 system 指令和历史错误响应，不以全文禁词篡改历史。首次、反馈新 revision、局部纠正均验证当前说明；`test_plan_revision_handoff.py` 回归真实 Recovery `revise_plan` 的冻结 seed、原事实和后续纠正请求。

正在运行的旧进程/已冻结 phase 不会被静默重写。本补丁需要新进程加载源码，并在新 phase 构造当前协议。本轮没有替用户启动进程或恢复任务。

## 生命周期纵向证据

原 session 的安全静态投影见 `evidence/session_projection.json`：第二轮六个 `planner_materialized` 被拒，task revision 2 与旧 Plan 来源 revision 1 不一致，旧提交仍被批准；action 后来被 Execute preflight 撤销，`execution_started=false`。这是未执行前的绕行，不是 WGS 运行报错。原统计 88 秒模型用时只适用于已记录的约 120 秒窗口，不是优化幅度或当前存活状态证明。

当前 HEAD 的真实反馈路径还会产生一次已有 `planner_plan_impact_review`，测试遵守这个路径，没有为了复刻旧日志而 stub 掉它：

1. 生产 Planner 提交 v1，真实 PlanReview 收到合成反馈，Consultant 提交 task revision 2。
2. 此时只有来源 revision 1 的 Plan。验证其旧提交不可审批，并把生产结构校验的六个错误交给真实 rejection producer，证明旧 Plan/receipt 保留而资格无效。这一组合是从实际反馈状态派生的组件反例。
3. 完整图继续原有 impact 调用；接着实际 Planner 返回六个非法值，生产 parser → rejection/context → scoped phase → typed selector 留在局部纠正。下次 provider 调用时，审批累计仍只有第一次反馈，Generate=0、执行=0。
4. 合法纠正后，只审批当前来源 revision 2 的新 submission，Generate 绑定该 Plan，无害 `printf` 进程恰好启动一次。

compact / verbose / off 三次固定脚本场景一致：Consultant=2、既有 impact=1、Planner=3（v1、非法候选、合法纠正）、Generate=1、真实短进程=1。新增专用模型调用=0。另有同 revision 新失败、旧失败后新成功、审批间到达新 UserTurn、迟到 provider、合成 checkpoint、重复提交/批准及强送旧 action 的反例。

证据 `lifecycle.json` 保留各次来源 hash、阶段身份、旧/新 receipt、typed outcomes 和执行计数。没有手工预填最终合法状态，也没有更改历史 Plan 的版本、hash 或批准时间以放行。

## 输入结论与 PTY

| 原 session 原因 | 当前合成复现 | 补丁保证与边界 |
|---|---|---|
| **UNKNOWN**：没有原始键盘/paste 事件；重复末句本身不证明串入 | baseline 生产 `_aprompt_user` → `_prompt_user` → PlainTerminal 在分块间超过旧 80ms 窗口后提交首行，尾部被审批读取消费 | 识别有开始/结束标记的 paste，按完整事务接收；CRLF 归一为单个行结束，尾空行留在任务里，结束后的提交 Enter 只消费一次 |
| 不推断用户是否主动重复 | 独立重复末句在当前审批下被正常当作反馈 | 不做文本相等去重 |
| 不推断历史空行来源 | 审批出现前已排队的尾句、空行、`y` 不能代表当前决定；空白 framed paste 也不能空行批准 | 保留为草稿；等待当前审批下新的明确提交。没有 flush/drain stdin，也没有按时间阈值丢字 |
| 无 framing 的字节流无法无歧义归属 | 不宣称能识别所有未标记粘贴 | plain 优先 `/paste`，正文后 `/end` 单独一行；仍保留单行用法。未标记且之后才到达的多段输入无法可靠自动归属，不能把它当作受保证的多行方式 |

`human_io.terminal_line_framing` 在单一 plain reader 期间保留 CR/LF 差异，使用 TCSANOW 恢复原属性，不冲洗输入；原审批按键语义保持。动态 TTY 使用原编辑缓冲区和 paste 事件；详情/Esc 不提交决定。EOF、未闭合 paste、取消不批准；原有非安全 legacy confirmation 的 Enter 规则未改。

生产输入 + 长代码审批 + 内核 PTY：1 条 baseline 负例，11 条正例（plain/TTY/off、LF/CRLF、显式多行、独立 Enter、重复反馈、详情/Esc、EOF、取消、两种终端的空白粘贴）。所有正例待审时决定 0/执行 0；批准场景执行 1，拒绝/EOF/取消执行 0；模型调用均 0。`evidence/pty/` 保存原始 PTY 字节、pending 屏幕、输入字节及原审批/执行事件。plain 子进程 PYTHONPATH 为空，不依赖 `/tmp` 的 prompt_toolkit；TTY 只使用既有离线依赖缓存，未安装真实环境。

## 实际验收命令与结果

仓库根目录执行；以下目录均是本轮新建。完整子进程 argv、每次失败/通过和来源 hash 在 `evidence/test_runs.json`，不是只有最终 PASS。

```bash
python tests/export_planner_boundary_fixture.py /tmp/biomind-pf1b-export --tree 1a54d2edd0cb2da1c1d0dcb3a5539f5fe6ab8d8f
python tests/cli_v2_support.py --export /tmp/biomind-pf1b-export --results /tmp/biomind-pf1b-results -q tests/test_planner_boundary.py tests/test_plan_revision_handoff.py tests/test_plan_result_contract_v1.py
# exit 0，35 passed

python tests/cli_v2_support.py --export /tmp/biomind-life5-export --results /tmp/biomind-life5-results -q tests/test_planner_boundary.py tests/test_planner_lifecycle_boundary.py tests/test_plan_revision_handoff.py tests/test_plan_results_purpose.py tests/test_consultant_skill_handoff.py tests/test_consultant_stop_resume.py
# exit 0，40 passed

python tests/cli_v2_support.py --export /tmp/biomind-life7-export --results /tmp/biomind-life7-results -q tests/test_planner_boundary.py tests/test_planner_lifecycle_boundary.py tests/test_plan_revision_handoff.py
# 幂等增量验收：exit 0，7 passed

python tests/cli_v2_support.py --export /tmp/biomind-inputblank2-export --results /tmp/biomind-inputblank2-results -q tests/test_input_boundary.py tests/test_approval_owner_lifecycle.py tests/test_approval_visibility.py tests/test_cli_v2_terminal_fallback.py
# 最终输入增量：exit 0，47 passed

PYTHONPATH=. python tests/replay_input_boundary_pty.py /tmp/biomind-input6-export /tmp/biomind-input6-tty-details --mode tty --decision details
PYTHONPATH=. python tests/replay_input_boundary_pty.py /tmp/biomind-inputblank2-export /tmp/biomind-inputblank2-plain --decision blank-paste
PYTHONPATH=. python tests/replay_input_boundary_pty.py /tmp/biomind-inputblank2-export /tmp/biomind-inputblank2-tty --mode tty --decision blank-paste
# 各 exit 0，所有检查 true；完整 12 条回放索引见 pty_index.json
```

如需重跑，请换成新的 `/tmp` 输出目录，exporter 拒绝覆盖既有目录。生产/测试源码 tree 与 commit 对应见 `evidence/manifest.json`。

### 未解决的原生崩溃

扩展命令 `python tests/cli_v2_support.py --export /tmp/biomind-input5-export --results /tmp/biomind-input5-regressions -q tests/test_plan_results_name.py tests/test_plan_results_evidence.py tests/test_plan_results_managed.py` **exit -11（SIGSEGV）**，已完成 21 个用例；停在 `test_managed_name_resolution_commits_original_computation_and_feeds_downstream[copy_to_exact-off-False]`，没有 Python 异常栈。该次余下用例不能算通过。

局部复核没有改变捕获模式：原基线 `51834d...` 的同一单例 exit 0 / 1 passed；补丁树的同一单例加 `test_plan_results_evidence.py`、`test_plan_results_managed.py` exit 0 / 18 passed。不能据此断言间歇性 native 崩溃已修复或确定由本补丁引入/排除。本轮未改原生崩溃专题；保留为 **UNKNOWN / 未解决**。记录见 `native_crash_pytest_results.json` 和测试账本。

早期 PTY 驱动也有超时：没有识别动态提示、用 LF 代替提交键、未等待详情实际打开等属于驱动握手错误；已修正并重录。没有把这些超时录像作为生产 PASS。一个旧 AP 测试原先 mock 80ms 收集器，已改为真实 framed 输入，原“/details 和 y 保留为反馈正文”断言不变。

## 证据自检

```bash
python docs/implementation/planner_input_boundary_20260912/verify_evidence.py
```

验证只读取本目录已提交的合成/脱敏证据，不加载业务状态。原输入、原 Plan/批准记录及 437 条无关工作区状态均未被本轮覆盖。真实 API、科学计算、原 session 恢复、原业务数据库：**NOT RUN**。
