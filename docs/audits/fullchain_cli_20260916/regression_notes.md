# 扩展回归的既有失败与测试更新

本轮先在完整相关测试中发现失败，再用 **BASE + 同一先存 overlay** 对照，保存了原始失败回执。以下 14 项在两边均失败，不能计为本轮新增回归，也不能计为通过。精确 nodeid 与两边回执见 `baseline_regressions.json`。

- 原有未跟踪的 `test_recovery_compaction_production.py` 有 12 项在当前基线同样失败。可观察差异包括首轮容量拒发、CAS 数量、压缩维护请求数、typed blocked 与旧 internal_error 预期。本轮没有将这些差异全部归因为测试过期，也没有为满足旧断言修改阈值、删减 Seed 或恢复旧路由；这些失败保留为未解决项。当前 Phase B 的 20 项维护/CAS/下一请求组合回归另行通过，证明其覆盖的当前路径，不替代对旧失败的进一步判断。
- `test_tool_checkpoint_failure_stops_before_next_model_boundary`：旧测试要求事件 sink 的 OSError 抛出 `RecoveryAgentError`；两边均未抛出。本轮未改这一持久事件错误边界，相关结论保留为既有未解决项。
- `test_secondary_faults_preserve_outer_base[writer_one]`：旧注入点 `_write_report` 已不是当前发布器实际写入入口，故仍观察到报告。新用例从生产停止入口构造真实受保护目标，验证未覆盖和内部诊断成功，未靠替换 formatter 证明文件保护。

另更新了已跟踪 CLI Generate 图测试的过期替身：当前 provider 接收 helper `marker` 而不是完整 `python_template`，Python heredoc 显式使用 stdin `-`；当前语法检查来源为 `check_executable_action` 且包含语法回执；计划审批使用既有 inline 展示，不要求模态窗口。完整响应解析器与候选错误投影对“整个响应”有 `$`/`response` 两种已有指针写法，分别检查为候选错误，模式对照比较实际审批/执行/Commit 等业务结果。原始反馈仍完整留在回执中。未修改生产接收器或审批语义来适配这些旧测试。

新专项/组合用例与既有适用回归的通过数量、排除项和结果均在 `test_results.json`。该记录明确区分专项通过与全量扩展测试仍有既有失败，不能解释为全仓零失败。
