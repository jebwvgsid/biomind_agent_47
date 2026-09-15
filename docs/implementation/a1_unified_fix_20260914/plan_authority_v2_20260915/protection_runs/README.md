# 保护回归结果

最终保护基表按 **155 个唯一 nodeid：154 PASS、1 个已存在的基线断言 FAIL**。若按包含 unittest subTest 的 call 回执计数，则为 159 条中的 158 PASS、1 FAIL。同一资源引用用例及其四个 subTest 共用 nodeid，不能把 159 条当成 159 个唯一用例。明细见 `final_unique_verification.json`。current12 已实际重跑 F1/F2 39 项，全部 PASS、rc0、source_changes=[]；其中实际普通、局部纠正和 Recovery 请求使用同一示例生产者。其余选取 current10 纠正/停止代表 5 项、current08 43 项、current07 59 项及 current09 取消 9 项的原回执，未重复计数。

最终 current14 对 current12 只在通用 Bash/script 示例分支增添一行安全引用已公开资源的赋值。旧 39 的实际 native 模式，以及唯一 legacy script 请求的 `sequence.search.local.blast.execute` 能力和 `tool=test`，均不进入该分支，故限定复用 current12 结果；未冒称 current14 重跑。142 个实际加载模块中有 141 个与 current14 字节相同，`step_effects.py` 为明确差分的第 142 个。新 V2 图测试未被这组加载。精确证明见 `current12_vs11_generate_examples_proof.json`、`current14_vs12_generic_bash_resources_proof.json` 及配套差分；current10 出口修补另见 `current10_vs09_exit_literal_proof.json`。其他分组以根逐依赖账本为准，不冒称整模块字节相同。

- current07：F1/F2、输入与审批、Recovery claim 重载、schema2 hooks、四种终态 summary hook 取得正式通过回执；旧 fixture/export 接缝失败原样保留。
- current08：明确能力 fixture 后，原 F3 29 例经五个小组全部通过，late-result 四例全部通过；取消例推进后因缺公开 task_declarations fixture 尚未进入场景。
- current09：补齐指定公开 fixture，并通过显式 pytest 插件把旧测试产物目录指向现有 guard 的新隔离目录，原取消九例全部通过；未改取消断言或生成成功状态。

唯一保留失败为 WGS 旧测试断言两个 Skill 都是 1.1.0。安全 BASE 与最终导出的相同 Skill 字节实际分别为 1.1.3、1.1.1，原断言字节及来源见 `baseline_wgs_version_assertion.json`。BASE export 未包含该旧测试，因此旧断言来自明确 BASE git 对象，不冒称 BASE 已运行过此测试。

另保留一次外部测试驱动中断：多个代理同时收到 hostclosed/143，原因 UNKNOWN。原 F3 批次只有 19 条已完成 call，无完整子进程退出回执，未计作整组通过；其后 29 个原 nodeid 已完整重跑。没有归因为生产、CPython 或已确认 timeout。

每组包含原 pytest/rc 回执、压缩 stdout、实际已加载模块哈希，以及适用的固定 provider/合成科学保存数据。`f6-preserved-*` 只压存既有 fixture 指定的批准脚本、执行日志、state 和报告，未归档整个 scratch。版本等价与整体结论以根最终账本为准，本册不声称真实外部模型或真实科学 E2E。

旧 legacy 与 native 预审批小例沿 `ger_fixture_support` 替换 `_ainvoke_node_llm`，证明真实请求组装、parser 与准入接口，不能单独证明模型 transport/register/consume 包装。普通/local/Recovery 的 `ProtocolGraph` 继承 F6 的真实调用包装和固定模型叶子。旧 22 图的固定模型另写候选，不构成新公开示例原样接纳证据；新通用示例与停止分支的完整验收由根及 incident 的 current14 专项提供。
