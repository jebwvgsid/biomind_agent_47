# required 依赖缺口到安装准备的完整入口

稳定检查点之后，真实图补测发现：Generate 因 required runtime 缺事实而在 provider 之前退出，runtime Recovery 在 UNKNOWN/UNAVAILABLE 终态直接结束，原有 Generate 安装标签入口因此无法覆盖这个触发。补测的两个预期失败及前后完整证据保存在 `evidence/dependency_entry_before_after.json` 指向的持久目录中。

现在，仅当前未派发科学 scope 的合法 runtime 缺口可发布依赖准备问题。用户可明确选择已有命名环境、准备安装或退出。未作选择时没有工单、安装执行或科学批准；选择安装只是进入提案，实际 Installer 内图仍对具体命令和目标产生第二个审批中断。父入口优先使用内图已持久化的最新 receipt，避免外层中断恢复用旧值覆盖已批准的执行证据。

同 prefix 准备成功的回执经父入口真实目标复验、Skill/software 两项登记后，调用既有 A3 准入。新 epoch 执行新观察，覆盖的 AVAILABLE 事实明确 supersedes 旧负事实，旧 evidence 全量保留；随后重新生成和批准科学代码，实际执行独立 `/tmp` tiny shell 并经 Commit 完成。无 attempt 的新 scope 及已有历史失败 attempt 已退役的 scope 均有独立覆盖。

`tests/test_unified_dependency_preparation.py` 五条真实图测试覆盖 UNKNOWN/UNAVAILABLE 未选择停点、两种同 prefix 已有环境恢复，以及 UNAVAILABLE 后显式安装的全链。最终完成时事实读取器返回 `current_managed_stop={}`、runtime diagnostic 为空、terminal status 为 completed；历史检查失败仍保留。

外部叶子使用离线模型/用户答复、受控 probe 进程、目标 healthcheck 和安装 runner；关闭可选 Consultant seed/preflight 与记忆旁路。Installer 图、问题身份校验、当前 state/scope、批准、两项登记、证据接受和 Commit 均运行真实实现，科学 tiny runner 是实际进程。没有软件安装、网络/API 或完整 WGS。

最后权限复核另外修正：TaskSpec 的 `forbidden_behavior.runtime_install` 只在实际进入 Installer 前禁止安装，不应阻止合法已有环境/人工完成的只读复验、登记重试、取消或拒绝。来源、目标、Skill/capability 和禁止目标环境约束继续验证。此边界在安装专项中单独覆盖；最终结论以 `evidence/cumulative_receipt.json` 的同一累计候选为准。
