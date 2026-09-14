# F1 完整公开示例的准入与运行值

当前普通、Recovery、局部纠正请求使用同一个 `generate_output_examples`。它消费实际 `current_action_capability`、artifact/runtime/workspace provider projection 和冻结 binding requirements，生成可原样解析并提交本步准入的完整 native 对象；当前 contig 输入路径、operation 和可确定的公开 handle 均来自该请求。缺少必需值、无唯一 handle 或新语义值尚未确定时，返回完整 blocked 示例，不填假路径、假 handle、0、False 或任意名称。

这里的两个正例含义严格分开。当前 action 示例通过的是**动作解析和本步准入**：科学计算函数仍明确抛错，直到模型提供实现；它不发布当前 observation，也未完成当前科学义务。独立 `synthetic_protocol_demo` 使用自己的 `synthetic_demo_matches` 声明，通过真实小进程计算得到 False 和空列表，并通过真实 helper/effect parser。它不是当前步骤的候选或 observation。F6 的固定模型另行编写真实合成科学计算，验证相同批准义务的执行/证据/Commit 正例；本文件不把三者混称科学 E2E。

native 提示现明确支持 code/language 或结构化 argv；同时提供时必须相符。原先被称为合法的静态 `/opt/...`、`/data/...`、`/results/...` 示例已删除。仅用于解释分类字段的 `facts_example` 明确标为不可执行形状模板，里面的占位值不是运行事实。

新增 `test_f1_protocol.py` 从真实生产图的实际 provider 请求中抽取动作示例，固定 provider 原样选择回复，不在 parser 前补字段。普通和局部修订读取冻结文本 seed，Recovery 同时核对其真实 JSON GenerateSeed；三条 native 路径均到达实际 `action_ready`。legacy 的实际 script 模式请求经正式 display projection/parser 到达同一准入结果。未知 genome 语义值的示例在实际第二步形成 blocked，经真实终态收口，诊断持久化为 committed。零个/多个候选 handle 的负例只修改公开投影副本以测试拒绝猜测，不声称它们来自一个真实已授权 workspace。

最终统一版本 final08 的 **6 个新增 protocol 场景全部 PASS**，包含在同次 **101/101 PASS** 的 helper 与公共调用方回归中；子进程 rc=0，导出运行前后无变化。当前证据入口为 [`unified_final/current_final08_evidence.json`](unified_final/current_final08_evidence.json)，原始请求、实际加载与回执已保存于 [`unified_final/final08`](unified_final/final08)。本次使用最终报告 origin、候选进展与 helper scope 源码，其中普通/局部纠正/Recovery/blocked 四场使用外部模型叶子，保留真实节点登记/消费及传输包装；不代表真实 API 或流式模型验证。

此前 [final06](unified_final/current_final06_evidence.json)、[final03](unified_final/current_final03_evidence.json) 的六项与 101 项结果及原始归档完整保留。当前结论来自 final08 重新执行，没有把旧 fixture 绕过节点调用包装的结果混作最终验证。

统一候选 final01 的六项 PASS 及原始请求仍保留于 [`unified_final/regression_evidence.json`](unified_final/regression_evidence.json)，final01→final02 的同字节证明仍保留于 [`unified_final/final01_final02_equivalence.json`](unified_final/final01_final02_equivalence.json)。这些是 helper scope 窄修之前的候选记录，不替代上述 final03 动态结果。

此前独立固定导出 **21 PASS**：6 个新增 protocol 场景与 15 个本轮 F1/F2 场景。精确版本、实际加载字节、所有开发结果和压缩原始请求/回执仍保留于 [`evidence.json`](evidence.json) 与 [`final_manifest.json`](final_manifest.json)。两次开发失败分别是新测试误用 `focus` 字段、误认为 Recovery 仍用 XML seed；原错误结果完整保留，均未通过修改生产错误处理来消除。

所有输入、状态、科学替身与进程均位于新 `/tmp` 隔离根。没有真实 provider/API、正式 VCF/FASTA/GTF、正式配置、原 session 或原批准脚本读取/执行。父版本与实际加载字节分开记录；最终统一 F6 验收由主账本统合。

legacy 模式一项仍使用原 `ger_fixture_support` 的固定节点响应替身，验证正式请求、display projection、parser 和本步准入，不计为真实节点登记/传输证明；独立 demo 是小进程/helper/parser 验证，没有模型调用。六项的替身位置在当前 evidence 中逐类列明。
