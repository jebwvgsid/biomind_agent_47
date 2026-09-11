# CLI V2 有限静态复核

复核范围为 UX1 commit `8ddf5bbb48fb1a84fe8f734be8b370134966da91` 与其后 UX2 工作区显示改动。以函数为定位依据；最终切片的独立离线测试和 commit 以本目录实施报告为准。本复核没有恢复业务运行、调用业务状态加载器、读取业务数据库、调用模型或执行科学任务。

## 发现与修正

1. `BioAnalystAgent._prompt_user` 新增 `/details` 循环最初将小于一秒的剩余等待时间传给 `safe_input`；后者转换为整数，零表示无限等待。因此一秒超时及接近截止时查看详情会改变审批等待语义。复核后源码已增加明确的截止判断，并向整数秒接口传递向上取整且至少一秒的剩余时间。到期后抛出原 `HumanInputTimeout`，沿原 waiting-state 持久化路径处理；不将查看详情当成审批答复。
2. `_schema2_recovery_event_sink` 最初仅根据 checkpoint 的 `status` 和 `truncated` 计数。现有 Recovery loop 在累计观察预算超限时可返回 `RECOVERY_OBSERVATION_BUDGET_EXCEEDED`，同时 checkpoint 仍为 `completed` 且未标记 truncated；直接计数会把受限观察显示成成功读取。复核后仅在显示副本读取既有 `observation.result.status`，将该状态及 `REQUIRES_NARROWING` 显示为部分结果，将 `NO_MATCH` 显示为空结果。原 checkpoint、ToolMessage、错误、预算和存档不改。`evidence.glob`、`skill.search` 的显示类别已区分为搜索。
3. Recovery 工具异常原本同时产生 `recovery_tool_failed` 与失败 observation checkpoint。当前显示出口对同一 tool request 使用相同失败显示身份，并以 observation checkpoint 承担普通完成显示；没有再从 `recovery_tool_completed` 额外生成完成行。

## 修后边界判断

- `OutputRouter.print_observation` 和执行/Supervisor 新标签只处理显示副本，原 `obs`、stdout/stderr、parser receipt、原始执行记录和返回给业务节点的消息仍由既有路径持有。局部 VALID、进程退出和正式完成的语义没有合并。
- `agent_core/cli_review.py` 生成单独的审批显示摘要与显示正文 SHA-256。它没有将此摘要或 hash 写回正式审批对象、执行定义或授权判断。TTY 详情使用原有待输入 Future，打开、返回及滚动不提交答复；弹层中 Enter 不批准。无法可靠概括作用范围时保留完整正文的可查看回退。
- Recovery 的 admitted 事件由 `RecoveryNode` 在现有 runtime 应用 directive 后产生。当前文案仅表示原事件携带的 action 已接纳，并说明后续仍由现有图处理；没有声称 Planner 已执行或完成。该事件没有 reason 字段，不能从后续 token 或显示文案补造具体路由理由。
- 对已复核差异未见 Recovery 上下文、provider 请求数、业务预算、路由条件、PlanTracker、StepEffect、CompletionGate 或审批 authority 的修改。工具结果分类函数观察返回值后原样返回对象；没有以显示状态替代正式状态。
- 工具聚合使用实际完成回执及显式身份。失败、未知、部分返回、未派发不进入成功计数；模型段落、审批、取消和方向变化结束相邻分组。上述结论是源码范围判断，不能替代实际生产事件路径的离线验收。

## 覆盖限制

复核时另发现既有 fallback 覆盖缺口，修后源码已为 Consultant 旧 KB 检索和 Generate 旧 SEARCH 的实际调用补充搜索类别与开始/结束显示；Generate 的原异常捕获保持不变，服务不可用时明确未派发。现有 retriever 的空字符串不足以可靠区分无匹配和失败，显示分类因此标记 unknown、不计成功；本轮没有声称已解决该返回值本身的歧义，也未修改 retriever 或原业务分支。

原 S1 终端录屏/完整文本未提供给本复核，不能据此认证原终端尺寸、精确可见帧或完整屏幕时序。静态 session 证据与本轮合成离线显示验证应分别引用；详见 `replan_causal_audit.md`、`replan_evidence_chain.tsv` 和实施验收报告。

## 最终组合回归的原生崩溃：有限只读检查

同一隔离提交树 `7164c04774838da92ee138b888b406e26019d0ef`、源码签名 `b1a9540392ac69d72b7c1207557701fd0db8a25da525c6bec20953011f1627a9` 的默认 pytest 捕获模式发生两次子进程 `SIGSEGV`（returncode=-11）。第一次组合回归已有205例通过，最后记录为 Generate完整TTY目标的start/setup；第二次49例独立回归已有40例通过，其中该TTY目标已在25.58秒通过，最后记录为下一项Generate Skill early-return的start/setup。不同的停止位置不能用于锁定某一个业务节点为崩溃根因。

只读检查确认了以下事实与限制，没有因此修改生产代码或既有测试清理流程：

- `tests/test_plan_local_preaction_convergence_v2.py:577` 的既有 `asyncTearDown` 依次停止patch、调用默认线程池 `shutdown(wait=False, cancel_futures=True)`、将 `loop._default_executor` 设为None，再删除临时目录。所用Python3.12的 `Runner.close` 本应调用 `shutdown_default_executor`；后者见None立即返回。因此这里确实绕过了默认线程池的正常等待。运行中的线程池工作可能越过临时目录清理边界，这是可核查的测试隔离风险，尚未证明它导致这两次段错误。
- `tests/test_cli_generate_public_integration.py:335` 的TTY helper在 `asyncio.run(invoke())` 返回后才设置completion；正常通过路径会等到worker内部Runner完成，而不只是看daemon线程存在。helper没有显式join；timeout/failure分支使用shield后也未终止worker，存在另一个清理边界，但不能把正常通过的目标直接描述成“图还在后台执行”。该fixture还在离开graph stream时显式 `await stream.aclose()`；已安装LangGraph的后台executor退出实现会等待其登记的任务。
- G fixture 的setup已把 `_observe_session_memory` 和 `_finalize_task_memory` 替换为无副作用的no_observe，并关闭root-cause analyzer。SessionArchive writer只在enqueue时懒启动；创建agent及registry条目不等于启动归档线程。`memory/session_archive.py:917` 在每次上下文中新建SQLite连接，finally关闭，没有发现该路径共享 `check_same_thread=False` 连接。因此目前不能把崩溃归因于“残留归档/SQLite线程”或“报告后台写入”。
- `PlainTerminal.close()` 只投递结束标记、没有join其daemon writer，这同样是可核查的生命周期边界；没有native fatal stack证明它造成SIGSEGV。没有把这些静态风险写成已确证原因，也没有为通过测试而修改旧fixture或改变业务收尾。

进一步诊断只对上述同源49例加入Python `-X faulthandler` 和pytest `-s`，源码、guard与合成环境范围保持不变。该次49例全部通过（121.24秒），源码漂移为空。70秒处的输出来自既有watchdog定时栈采样，**不是fatal栈**：当时仅见一个idle默认executor、处于JSON解码→PlanState读取的G runtime线程、以及等待selector的主线程，没有归档/报告线程出现在该采样中。该目标随后通过；不能从这个非fatal采样认定JSON解析、SQLite或renderer是根因。

诊断结果保留于 `/tmp/biomind_cli_v2_20260912/bundle-legacy-faulthandler/` 的 `receipt.json`、`pytest-results.json`、`invocation.json` 与 `stdout.txt`；两次捕获模式的停止记录分别位于 `bundle-candidate-results/`、`bundle-legacy-recheck/`。本次结论是：诊断49例通过，但两次默认捕获模式SIGSEGV的原因仍为 **UNKNOWN**，没有取得它们的fatal native stack，也不能以诊断通过补写默认捕获组合回归“全部通过”。按有限排查范围冻结，不进行无依据重跑或扩大业务修改。
