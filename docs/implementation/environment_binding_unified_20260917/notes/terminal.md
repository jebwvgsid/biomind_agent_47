# C1/C2：确切停止状态与本次诊断

生产修改位于 `agent_core/workflow/managed_stop.py` 和 `agent_core/cli_lifecycle.py`。既有 `runtime_recovery_reporting.publish_report` 的所有权/旧文件保护已通过验收，不重复改造 writer。

终态持久化继续先经过 `_matches`、Plan/step/attempt 身份和 CAS。执行后的终止仅在精确 attempt 已有结束状态、`active_execution/current_attempt` ownership 已释放时同步对应未提交步骤；新的 active attempt、别的 Plan/step、仍运行或仍被持有的 attempt 不受这段同步影响。已完成步骤和精确 Commit/semantic consumption 回执受保护，其余 pending 步骤及历史 attempt 不变。这个 writer 不负责杀进程或释放 ownership。

`inspection_failures` 保留历史。当前问题投影依据同一 episode 的真实 ordered observation receipts，仅把后续成功读取相同 immutable `*:sha256:<digest>` 引用的错误列为 resolved，并保留成功 request/tool/digest。更早的成功、不同引用、不同 episode、相同可变文件名均不能清除当前错误。

CLI 对明确结构化 stdout 的 `missing_commands` 显示“该次脚本报告”，结合精确 attempt/action 的 runtime manifest 显示空 bindings；不会将模型的缺能力猜测提升为软件未安装的事实。成功 publication receipt 的本次 report 与内部诊断绝对路径可见；受保护旧目标不冒充本次报告。

新进程验证 **43 passed**：新终态场景 18 项、既有 scope guards 6 项、runtime recovery reporting 19 项。真实 graph 的停止/持久化/报告写入和 CLI 投影保留；外部模型与科学进程仅在既有明确边界使用合成 fixture。报告 conflict 时验证旧报告 SHA256 不变、本次内部报告真实存在且路径可见、重复收尾幂等。源码与受测候选 hash 一致，见 `evidence/terminal_results.json`。

`tests/fixtures/unified_terminal_recorded.json` 只读摘录事故 `session_1789628497` 的有关字段及源文件 hash，测试不打开原 session。真实历史回放确认 picard 的限定诊断、空 runtime bindings、已解决的引用类型错误和本次内部报告路径。原 session、原报告与历史 attempt 未改写或恢复。
