# F4 AskUser 退出与 F5 Plan 默认正文

F4 的两行路由补丁让已由生产 typed wrapper 形成的 `harness_error` 直接 END。缺答复 receipt 原本就会 END，继续保留；`question_missing` 和真实 question/answer-turn receipt 继续回 Consultant。没有增加 broad catch，也没有改写 AskUser 的语义诊断、取消或 interrupt。

F5 由生产 PlanReview 给 `ReviewDisplay` 传入显式 `kind="plan"`。默认 compact 复用原本的冻结 `body`：TTY 将完整正文交给现有只读 TextArea，PgUp/PgDn 浏览；plain 在同一个 presentation/write/flush 边界打印完整正文。F2 和返回复用这个对象。`input_ready`、显式 Enter、owner、冻结 submission、receipt/CAS、代码审批入口都继续由原代码负责。

逻辑提交范围：F4 为 `agent_core/workflow/runtime_graph.py` 的 `route_ask_user_outcome` 与新增 `test_f4_ask_user.py`；F5 为 `agent_core/cli_review.py`、`cli_plain.py`、`cli_terminal.py`，以及 `bio_agents.py` PlanReview 的单个 `kind="plan"` hunk 与新增 `test_f5_pty.py`。共享 `bio_agents.py` 的其他实现和保护 overlay 不属于这两片。

验证直接使用安全导出中的生产代码。F4 构造正式 BioAnalystAgent，调用已注册回调；异常穿过真实 wrapper、隔离图条件边和正式 managed finalizer。真实 AskUser 图 interrupt 后的 EOF 使用新建闭合管道进入 `_prompt_user`，没有恢复图或创造回答。F5 从新 task 和正式用户提交开始，以固定 Consultant/Planner response 驱动真实 `_app_for_task`，经过正式 Plan 编译、冻结、审批输入、持久化 receipt；在 PlanReview 返回后停止采集，Generate 未派发。

F5 是真实内核 PTY。正文含 12 个带中文/空格的步骤及长成功条件；默认视图完整文本、F2 包含的完整冻结 body、显示 hash、一次当前 owner 的确认、receipt 的 plan/version/submission 全部关联断言。renderer callback 抛错场景通过正式 `run_terminal → use_plain`，验证原 Future 仅转交一次，plain 持续呈现同一正文。其余场景包括滚动/resize、明确 Enter、拒绝与反馈、paste、呈现前排队 Enter、EOF、取消，以及等待期间语义/非语义变化。

替身边界明确：模型只返回固定 JSON；Plan UI 使用旧安全 fixture 的合成 Skill 和前置能力可用返回，相关可选旧 memory hook 关闭，此处不作为 hook 组合验证。旧 fixture 的自动代码审批已撤销，真实代码审批中间件入口被计数观察；尝试数来自正式 provenance state。科学算法、真实 provider、正式配置、宿主 profile 与原事故 session 均未运行。schema2 hook 主链组合由本轮独立 F6/hook 验收记录。

统一固定导出中，F4 新增 6 项与未修改的 INPUT/审批可见性/owner 43 项合计 **49 PASS**（final01）；F5 的 **14 个真实 PTY/plain 场景全部 PASS**（final02）。新证据入口为 [`unified_final/regression_evidence.json`](unified_final/regression_evidence.json) 与 [`unified_final/final02_pty/evidence.json`](unified_final/final02_pty/evidence.json)。final02 每场实际加载 117 个模块全部匹配 manifest，父与子测试均为相同 `cba6d096…` 字节，运行前后导出无变化。原始终端、完整冻结视图、正式 receipt 与合成 durable state 均以可还原的 gzip 保存，不以截图代替正文。

final04 另补受影响旧 CLI 调用方 **25/25 PASS**：`test_cli_v2_display.py` 的 24 项，及 `test_cli_model_progress_integration.py` 的 1 项（含 plain/off/timeout/broken 四模式）。原断言未改，两个隔离进程均 rc=0 且源码前后无变化，见 [`unified_final/final04_cli/verification.json`](unified_final/final04_cli/verification.json)。这组是原 CLI 组件/事件接线回归，不能另称 25 场真实 PTY 或完整生产图，也不并入原 49 和 14 的计数。

final05 对旧 model-progress 组件又作定点复验，**1/1 PASS**，四模式均完成，131 个实际导入路径对应源码与冻结清单一致，见 [`unified_final/final05_cli/verification.json`](unified_final/final05_cli/verification.json)。其余 F4/F5 与 display24 的复用证明见 [`unified_final/candidate05_reuse.json`](unified_final/candidate05_reuse.json)：除动作解析和候选进展两个指定函数外，生产源码字节未变；13 个实际入口片段同 hash，F4 只走 AskUser/终态、F5 图更新止于 PlanReview，display24 的 9 个导入源码均相同。原回执保留实际 final01/final02/final04 加载版本，没有改写为 final05 的全文件 hash。

final06 的当前复用证明见 [`unified_final/candidate06_reuse.json`](unified_final/candidate06_reuse.json)。它重新核对上述入口/显示范围，并单列 final05→final06 仅候选进展函数变化：model-progress 组件的真实调用是 RecoveryAgentLoop、事件持久化与 PlainTerminal，未进入候选进展判断；其余实际导入源码逐一同 hash。四模式的实际回执仍是 final05，不冒称该组件或旧 UI 子进程加载了 final06 的变化文件。

final08 的 F5 与旧 CLI 当前复用证明见 [`unified_final/candidate08_display_reuse.json`](unified_final/candidate08_display_reuse.json)。相对 final06，唯一生产变化是在 `build_managed_stop` 的无 packet 且 `boundary.node=generate` 条件内投影报告来源；14 场真实 F5 更新均止于 PlanReview，旧显示与 Recovery 事件组件也没有该调用路径。证明逐一保留实际导入 hash；不宣称在 final08 重跑了 14/25。F4 涉及同一终态 builder，由主账本在 final08 独立重跑 49 项，不从这份显示复用证明得出 F4 通过结论。

final01→final02 的 343 个生产与公开资源逐文件全同 hash，见 [`unified_final/final01_final02_equivalence.json`](unified_final/final01_final02_equivalence.json)。final03 F2 scope 窄修的逐模块、13 个入口片段与顶层函数核对见 [`unified_final/helper_scope_reuse.json`](unified_final/helper_scope_reuse.json)。该修订属于 Generate/helper；本组实际停止于 PlanReview，provider 只有 Consultant/Planner，后继尝试数及代码审批调用数均为 0。F4 只走 AskUser/终态分支。变化函数不在本组调用边界内；此处仍保留实际加载的 final01/final02 字节，不冒称 UI 子进程加载了 final03 的新 helper。

final01 的 reject 曾由父测试将第二次 Enter 发给尚未更新的旧 ready frame，引发自己创建的子进程超时（rc=-15）。生产正确保留反馈 draft、拒绝过早确认。仅新增测试父端改为等待新的 pending_id、ready 和输出稳定；完整差异、失败记录、修正后结果见 [`unified_final/final01_pty/evidence.json`](unified_final/final01_pty/evidence.json)。final02 的 14 项已全部使用修正后的同一父测试重跑。

此前独立候选的 49+14 结果仍完整保留于 [`evidence.json`](evidence.json)、[`final_export_manifest.json`](final_export_manifest.json) 和 [`entry_regions_reuse.json`](entry_regions_reuse.json)。这些是开发及复用记录，不冒称加载过后续文件。

`development_runs.json` 保留开发期间的精确结果，包括一次 F2 整个面板等于 body 的不准确测试假设；F2 原有标题/提示/诊断使该假设失败，生产正文和 receipt 没有失配。固定导出的正文、F2 全文、当前 owner 和正式 receipt 联合断言已通过。

复现入口（新 `/tmp` 根、已知解释器、非登录 shell）：

```bash
/public/home/liusiqi/anaconda3/envs/biomind/bin/python tests/a1_unified_fix/export_and_run.py /tmp/new-f45-run --test tests/replay_cli_v2_pty.py --test tests/ger_fixture_support.py --test tests/test_plan_local_preaction_convergence_v2.py --test tests/test_approval_owner_lifecycle.py --test tests/test_approval_visibility.py --test tests/test_input_boundary.py --test tests/test_cli_v2_display.py --run -s -q tests/a1_unified_fix/test_f4_ask_user.py tests/test_input_boundary.py tests/test_approval_visibility.py tests/test_approval_owner_lifecycle.py
/public/home/liusiqi/anaconda3/envs/biomind/bin/python tests/a1_unified_fix/test_f5_pty.py --export /tmp/new-f45-run/export --results /tmp/new-f45-run/tty-approve --mode tty --scenario approve
```

PTY 的其他 scenario 为 `fallback`、`semantic`、`nonsemantic`、`queued`、`blank_paste`、`feedback`、`reject`、`eof`、`cancel`。plain 可用 `--mode plain`。父进程只加载 stdlib/已有纯测试 PTY 支持；业务子进程始终经原 allowlisted guard，显式最小环境，超时仅结束自己创建的子进程，不开放正式资源。
