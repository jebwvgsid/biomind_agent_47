# Recovery 专属 generation claim 的重载补证

**最终候选 08：1 PASS，34.96 秒，实际退出码 0。** 签名 `332a06e803cba6fef3e54d2648fef1f05703c1f8b3656637725a6a674934c5f9`，基线提交 `dbaa3f8e11bc56524ba2477c986d722657bf2b0c` 加保护 overlay；457 个导出文件的测试前后 hash 没有变化。04、05、06 的真实传输回执分别保留为先前候选，没有覆盖；07 仅导出未测。

前置由正式图生成：Consultant、Plan 与审批后运行一次真实新建脚本，得到 rc=7；原 Recovery owner 产生 `retry_step`，进入第二次 Generate。当前驱动保留真实 `_ainvoke_node_llm`、`_ainvoke_llm`、登记、消费、记录和重试/失效检查，只替换外部模型叶子。四次主节点调用（Consultant、Plan、普通 Generate、Recovery-generated Generate）均有实际登记与 consumed 回执；此单例没有穷举传输失败时序。

待决时，第一个独立子进程通过正式 claim API/CAS 持久记录 `claimed → outcome_unknown → claimed`：同一目标身份、预算未退，没有缓存成功或执行资格。它验证未知状态记账，不额外派发远端请求。

固定回复完成后，在真实 `provider_completed` 保存返回处停止消费。第二个独立子进程重入真实 Generate handler，禁止再次调用模型入口；结果为缓存候选 `action_ready`、同 claim `action_adopted`，模型调用为 0，预算未变。没有第二次代码审批、科学执行或 Commit。禁止模型入口只是确认缓存复用的哨兵，并非替代本次已完成的真实传输实现。

Recovery 使用 `recovery_state.generation_claim/history`，普通 Generate 使用另一组 `candidate_repair_state.provider_claims`。前者明确记录 `at_least_once_without_provider_idempotency`：远端完成未知时可能按同 target 重发，不能说 exactly-once。这与科学 Execute 结果未知后的重跑授权不同。

[最终账本及原始回执](f3_recovery_claim/verification.json)保存两个独立进程、状态与预算、实际新任务失败 attempt、传输回执和精确加载版本。驱动完善前的首轮 PASS 独立保留为旧组件范围，没有被改写成传输层通过。原 session、真实 provider、真实科学工具及全部 OS 故障窗口不在本次证明范围。
