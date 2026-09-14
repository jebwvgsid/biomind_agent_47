# 最终公共调用方回归

当前结论指向 [候选 08 实际回执](final08/README.md)：39 项独立用例为 **38 PASS / 1 既有断言 FAIL**，另 2 subtest PASS；457 个导出文件前后 hash 一致。候选 [05](final05/README.md) 和 [06](final06/README.md) 的全部结果独立保留。候选 08 在报告来源修正后重新执行全部五组，没有用旧回执复用替代。07 仅导出且没有运行测试。

[preflight.json](preflight.json) 列出安全 nodeid、Python 依赖、禁止读取/导出的旧证据路径、替代用例和可复用的原验证回执。实际运行结果使用统一最终源码 export，另行登记；准备清单不是通过结论。

安全回归包括纯合成的 candidate claims、Generate/Recovery handoff、cross-layer correction/delivery、capability prompt、provider payload 的前两项，以及无事故 fixture 依赖的 Planner lifecycle 和四项 Recovery 组件测试。最长的多展示模式 lifecycle 用例单独运行，每个隔离子进程保持现有 180 秒上限。

其余依赖旧事故 JSON 或其他 benchmark 原 session 的用例标为 `NOT_RUN_SAFETY`，不通过补导出、改断言或新 skip 标记制造绿色结果。旧九项 fixture 失败的原回执保持历史范围，不冒称现版本已经修好或仍必然失败。

取消 pre-dispatch 和原子保存窗口复用最新 Plan 已记录的两个精确调用回执。回执在 `all_test_results.json.gz` 中的指针为 `/acceptance-current-combined-pytest.json/142` 和 `/acceptance-current-combined-pytest.json/146`；相关 Planner、取消、持久化函数和模块与 A1 当时加载字节相同。`bio_agents.py` 的旧 Git blob 先经完整 hash 核对等于 A1 加载文件，才用于函数字节比较。审批展示函数本轮已变，单独由 F4/F5 新用例验证；不据此宣称原 237 个文件在本轮修改后仍全部相同。

本轮 F3 已用新的正式 Plan 图验证 claim/receipt 重投、未知调用、冻结预算、独立进程重载和取消后的 late-valid/invalid/blocked；F6 负责当前候选的新审批、真实小进程执行、证据、Recovery 和 Commit 组合。旧组件 fixture 中直接给出的状态与科学能力替身只按组件边界解释。

首次统一源码的历史回归已完成：[结果与原始回执](unified_final/README.md)。39 个独立用例为 38 PASS / 1 既有断言形状 FAIL，另 2 个子测试 PASS；所有已执行组都无源码漂移。

F2 实际作用域修复后的最终候选 03 已重新执行同一 39 项：[候选 03 回执](final03/README.md)，结果仍为 38 PASS / 1 既有断言 FAIL，所有已执行组均无源码漂移。

最终候选 04 仅改变 F6 测试驱动并新增 Recovery claim 测试，五组公共回归实际加载的全部依赖逐文件 hash 相同：[03→04 精确复用证明](final04_public_reuse.json)。这是旧回执的限定复用，不是新跑一轮。
