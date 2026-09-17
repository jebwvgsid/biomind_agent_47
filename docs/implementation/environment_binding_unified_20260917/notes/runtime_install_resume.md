# B1/A3 同 prefix 安装后继续发现

基于稳定生产提交 `2e70f28` 的增量修复。该补充不安装软件、不恢复旧 session，也不执行真实模型。

## 触发与结果

首次 Generate 的 required runtime 缺证据，尚无 action attempt；runtime Recovery 已进入 `unresolved` 或 `dependency_blocked`。用户显式准备同一环境后，prefix inode、Skill 和 subject fingerprint 可以不变。原 `prepare_generation` 直接返回，`create_request` 重用旧终态 episode，无法发起新发现；已有 `UNAVAILABLE` 事实还会在发 probe 之前再次阻塞。

现在，只有当前精确工单的 `available` receipt 与 request 全文 hash、冻结 generation source、Skill、capability、目标环境一致，且授权摘要尚未消费时，才为这个终态 scope 推进一次 epoch。没有实际 attempt 时事件的 retired 集合为空；若前一合法 epoch 已退役历史失败 attempt，新事件继承这些已验证排除项，不重复修改旧 provenance。普通 Generate、未知/失败回执、旧 source、不同 target、已消费工单都不能重置预算。

新 epoch 的 existing prepared event 记录旧 episode、精确 subject、required facts 和可被续验取代的旧 evidence IDs。Recovery 仅据这次授权进行新的受限观察；实际新观察成功且覆盖某事实，才通过既有 `supersedes` 语义取代列明的旧事实。旧 record 内容保持不变。未知、失败或未覆盖的事实保留原有负结论，不形成 ready，也不覆盖别的 Skill/环境事实。

`installation_generation_replay` 是父安装入口使用的纯读取 helper。它仅识别精确已消费回执、当前 source 除该次 epoch 外完全相等、无新 attempt/owner 的幂等重放，不签发新授权。

## 验证

新增 `tests/test_unified_runtime_preparation_install_resume.py` 的 13 项场景：

- UNKNOWN 和 UNAVAILABLE 各自经历正式批准 Plan、真实 store/episode、同 subject 已验证工单、fresh epoch/request、新观察接受；原 episode、旧 evidence 内容不变。
- 无回执、未 available、hash 不同、源 epoch 不同、目标不同均不能重开终态 scope。
- 新观察 UNKNOWN、execution_error 或仅覆盖部分 required facts 时，原负事实继续阻塞；部分覆盖的正事实可被接纳。
- 已消费工单重放不重复发 probe/推进 epoch；后来 attempt、epoch、active owner 不可继承该幂等例外。
- 旧 empty-required action → 正式新版本批准 → 合法退役 → UNKNOWN/UNAVAILABLE 终态 → 同 prefix 准备 → 新发现，历史 attempt/actions 全量保持等值，新 epoch 不复活旧所有权。

以上与 `test_unified_runtime_generation_authority.py` 和既有 `test_runtime_evidence_recovery.py` 合跑 **62 passed，5 subtests passed**。真实测试核心为 Plan/approval/store/CAS、runtime Recovery、immutable evidence 接纳与投影；外部观察和已验证安装回执是受控夹具，未替换被测核心。父安装入口、AskUser、Generate、HITL 和实际 tiny Execute/Commit 的完整图验证由主任务的独立集成测试记录覆盖。

结果、源 hash 和命令见 `evidence/runtime_install_resume_receipt.json`，源测试进程位于 `/tmp/biomind-r1-tXsGVj/review-candidate`，通过 `offline_managed_bootstrap.py` 且设置 `BIOMIND_DISABLE_DOTENV=1`。主任务最终累计冻结与全量集成另行记录。
