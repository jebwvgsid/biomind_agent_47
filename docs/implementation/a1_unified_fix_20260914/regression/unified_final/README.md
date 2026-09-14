# 最终统一源码的公共调用方回归

统一加载源码签名 `8a9c97461b19f769a10afbcf958b1b9cbd1c33f7a087290378cd95907af7e774`，基线提交 `8bf302c37bb38259d4878524ddc6e2d0e2a27e60` 加保护 overlay，共 451 个文件。五个新隔离进程都完成了运行前后文件 hash 校验，均无漂移、无 watchdog 超时。

| 组 | 独立用例通过 | 失败 | 实际退出码 |
| --- | ---: | ---: | ---: |
| shared | 18 | 0 | 0 |
| prompt | 12 | 1 | 1 |
| Planner lifecycle full | 1 | 0 | 0 |
| Planner lifecycle stale | 3 | 0 | 0 |
| Recovery pure | 4 | 0 | 0 |

合计 **39 个独立用例：38 PASS / 1 FAIL**；prompt 另有两个通过的 unittest 子测试，不能重复计入独立用例数。

唯一失败是旧 `test_nonempty_capability_uses_complete_production_message_safety` 的整字典断言。正式模型替身调用一次，四个预期布尔值相符，但 `GenerateInvocationAudit.to_dict()` 还返回 `generate_call_id`、`scope`、`source_ref`。该类与实施 BASE、BASE 加原保护 overlay 的字节相同，失败断言函数与 BASE 字节也相同，故归类为已有断言与返回形状不一致。整个测试文件存在其他版本差异，本结论仅依据精确类和断言函数字节。未修改旧断言或把失败转换为 skip/xfail。

[verification.json](verification.json) 保留各组目标、实际退出码、逐用例结果指针、hash、未运行范围及边界；[preexisting_assertion.json](preexisting_assertion.json) 保留静态归因。每组子目录的 `pytest-results.json` 包含原始失败，`stdout.txt.gz` 是未经内容改写的完整原始输出压缩件，`invocation.json` 记录隔离启动器明确提供的环境，`run_exit.json` 是实际进程回执。

依赖旧事故或其他 benchmark fixture 的组仍为 `NOT_RUN_SAFETY`，详见[预检账本](../preflight.json)。pre-dispatch/原子保存取消窗口仅按其中精确字节与旧回执范围复用；当前 Generate late-valid/invalid/blocked、claim 重载等由本轮 F3 新合成正式 Plan 用例覆盖。旧四项 Recovery 单元测试的直接组件状态不作为完整链证明，固定 provider 和小进程也不代表真实模型或科学 E2E。
