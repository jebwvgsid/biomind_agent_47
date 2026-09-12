# CRASH 有界后续（冻结 CLI V2 源码）

结论：原捕获模式 SIGSEGV 已缩小到四例顺序复现；尚未取得致命堆栈，根因 **UNKNOWN**。没有提交生产或旧测试清理补丁，也没有把 `-s` 或诊断模式通过称为修复。此结论不阻止其他业务切片的独立验收，但不能据此声称整体完全稳定。

## 来源与字节检查

复用了前轮两个异常记录、`verification_history.md`、`review_notes.md` 和 `51da2f7e680aa041dd7d51364e66c3949822f03e` 的六个测试适配 diff，未重跑大矩阵。冻结导出为 `/tmp/biomind_cli_v2_20260912/bundle-candidate`，Git 源树 `7164c04774838da92ee138b888b406e26019d0ef`，158 文件的清单签名为 `b1a9540392ac69d72b7c1207557701fd0db8a25da525c6bec20953011f1627a9`。

历史 `source_unchanged=false` 对应崩溃后缺少 `SOURCE_UNCHANGED` 尾部标记，应该解释为 **not_checked**，不是确认字节变化。父进程本轮重新逐项核对保留的 158 文件与清单，状态 **unchanged**。每次新子进程退出后父进程又独立核对；即使子进程 `-11`，仍得到明确结果。未从这个布尔值推测原仓库被修改。

## 四例缩小复现

顺序保持原异常记录中的相对顺序：

1. `tests/test_cli_cp_public_integration.py::test_cli2_actual_consultant_planner_local_turns_tty_plain_off`
2. `tests/test_cli_generate_public_integration.py::test_cli3_legacy_process_fixture_preserves_existing_effect_contract`
3. `tests/test_cli_generate_public_integration.py::test_cli3_actual_generated_envelope_approval_process_commit_equal_old_tty_plain_off`
4. `tests/test_cli_generate_public_integration.py::test_cli3_generate_skill_early_return_uses_real_context_tool`

未证明这四例是不可继续缩小的组合。最后一次事件只说明进入了第四例 setup，不能因此把第四例或其最后函数认定为责任函数。

同一个解释器、依赖目录、导出源码、两个原 audit guard、合成环境配置和上述顺序不变。仅将结果路径隔离到新的 scratch。首次沿原 runner 直接运行；第二次保留 capture，只将 Python 致命故障输出指定到独立且始终打开的文件；第三次保留该诊断，仅增加 pytest `-s`。诊断 wrapper 不修改冻结源码，不改变原依赖导入顺序，等原 bootstrap 自然导入 pytest 后才附加 sessionstart hook。`all_threads=True` 是该解释器支持的选项，没有假设新版 `c_stack` 参数。

原 Reporter 的 start/setup/call/teardown 事件和子进程实际退出分别记录在 JSON。独立文件在 pytest configure 之后启用，描述符贯穿测试期，不依赖 pytest capture 的临时 stderr；日志明确记录启用标记。原 70 秒 watchdog 没有移除，其非致命采样不作为 SIGSEGV 根因。

实际结果（每行独立，不累计 PASS）：

| 条件 | 子进程退出 | 完成 call | 耗时 | 父进程源码核对 |
|---|---|---|---|---|
| 原 capture | SIGSEGV / -11 | 3 passed；第四例未完成 | 95.02s | unchanged / 158 文件 |
| capture + 独立诊断文件 | 0 | 4 passed | 64.29s | unchanged / 158 文件 |
| 相同诊断 + `-s` | 0 | 4 passed | 61.91s | unchanged / 158 文件 |

两次诊断文件都只有启用标记（72 字节），没有 fatal 堆栈。一次诊断 capture 通过不证明更换输出描述符修复了崩溃；没有足够因果证据定位具体库或清理路径。

实际运行：

```bash
python /tmp/biomind_plan_results_20260912/crash/run_bounded.py capture-original
python /tmp/biomind_plan_results_20260912/crash/run_bounded.py capture-diagnostic
python /tmp/biomind_plan_results_20260912/crash/run_bounded.py nocapture-diagnostic
```

每个输出目录保留 `invocation.json`（真实命令及仅合成环境）、`pytest-results.json`（阶段事件）、`receipt.json`（退出与父进程 hash）、`stdout.txt`；诊断目录另有 `fatal.txt`。同名目录已存在时 runner 拒绝覆盖，复跑应使用新隔离目录。逐次结果见本目录 `acceptance.json` 的 crash 记录，不同运行的 PASS 不相加。

## 六个旧测试适配对账

| 文件 | 适配 | 保留的业务边界 |
|---|---|---|
| `test_cli_model_display.py` | 逐工具计时断言明确选 verbose；增加完整详情退出 helper | 公开说明 flush 先于工具、超时与取消断言仍在；helper 验证详情已存在，在 owner loop 关闭后返回原审批 stub |
| `test_cli_model_recovery_tools.py` | 旧命名工具逐行断言明确选 verbose | 原工具数量、返回、上下文、调用顺序与超时不变 |
| `test_cli_model_progress_integration.py` | 旧完整工具文字用 verbose | 原模式间业务与失败一致性断言不变 |
| `test_cli_cp_public_integration.py` | TTY fixture 返回原批准答复前关闭新版详情 | 原审批/执行/Commit 零次断言、调用数和真实消息未删除 |
| `test_cli_generate_public_integration.py` | 相同 TTY 详情适配 | 原 plain/off/legacy/TTY 返回值等价、原审批字节、执行与 Commit 断言未删除 |
| `test_cli_final_response_regression.py` | VALID 文案改为局部检查，并增加正式完成另判提示 | 原局部状态、证据和单次模型调用断言保留 |

旧 `8 failed, 41 passed` 的改动符合显示合同变化，未为通过而删除业务/审批/证据断言。已知默认线程池 `shutdown(wait=False)`、writer 不 join 等静态清理边界仍只是风险线索；本轮未取得其与 SIGSEGV 的因果证据，不把猜测写成已修根因。

所有测试使用原严格离线 guard、fake provider 与原已白名单的无害小进程。不读原 session/原数据库，不调用真实模型/API/WGS，不读启动脚本、密钥或敏感配置。未安装调试依赖。
