# 最终候选 03 的公共调用方回归

**39 个独立用例：38 PASS / 1 既有断言 FAIL**；另两个子测试 PASS。五组都完成，实际退出码分别为 shared 0、prompt 1、Planner full 0、Planner stale 0、Recovery pure 0；453 个加载文件的运行前后 hash 均未变化，没有超时。

加载基线为 `a64ca7652c74f505242620535788524d3f365241` 加保护 overlay，签名 `8ffbd5ed701df66b4079d6627fb15fdffcd131928be7303b4eeb2efda49dffd0`。本次在 F2 scope 窄修后按同一清单重新运行，没有拿旧候选通过替代新公共函数调用方验证。

唯一失败仍是旧 provider payload 测试的整审计字典断言；预期四个布尔值之外，生产返回原有的调用身份、scope、source_ref。生产类和精确断言函数与实施基线/候选 01 相同，见[原归因](../unified_final/preexisting_assertion.json)及[本次预检](preflight.json)。未改旧断言、添加 skip 或导入旧事故 fixture。

[验证账本](verification.json)保存独立用例计数、原始失败指针和 hash；每组目录保留原始 `pytest-results.json`、`run_exit.json`、`invocation.json` 和未经内容改写的 `stdout.txt.gz`。未运行的旧 fixture 组与科学/真实 provider 限制保持独立记录。普通 Generate claim 的新 unknown/reload 验证不自动覆盖使用另一既有 generation_claim owner 的 Recovery 全部窗口。
