# session_1789734892：预检登记与候选衔接修复

## 结论及证据边界

本次故障有三段：第一次预检 rc=0 后缺少正式产物选择/发布；随后修正丢失 helper 资源声明，第二 attempt 在启动 helper 前 rc=2；此后四份候选出现不同的响应载体/结构错误。最终由用户取消，不能归因为 API 超时、累计观察 96k、上下文不足或修正预算耗尽。

审计对象为 `/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789734892`。只读取了已有记录，没有恢复该会话、读取正式输入内容、调用模型/API、运行真实预检/WGS/BWA、安装软件或 push。离线回归中的执行端只产生标记为 synthetic 的回执，或重放已有回执；真正运行的子进程是框架的临时证据复制程序。它们不构成新的科学分析结果。

开始时 HEAD 为 `6b52faed41e0c9e7efedf959aaf4f5ff264f37b0`，包含 `81f783eb9b4da4d75d5d35d1279839d855167d01`。保留既有计时本地留档、普通模型请求结果精简、256k 输入软阈值及累计观察统计不阻断行为。

## 定点核对

| 接口 | 原始证据 | 已确认事实 |
| --- | --- | --- |
| 预检 → 正式结果 | `attempts/attempt_3e44cd9ef47e9a621a77/{stdout.log,preflight/checks.json,preflight_evidence_binding.json,execution_policy_manifest.json}`、冻结计划及执行记录 | 四个 FASTQ 前缀和参考头部共五项有限检查通过；不是全文件验证。原始证据在该 attempt 的 preflight 目录。冻结正式 artifact 为 `plan-artifact-680a67bfd6c885c7a01a89da`，路径是记录中精确的 `…/session_1789734892/plan_artifacts/00.input_check`，没有选择、发布并登记；首轮 `OUTPUT_SELECTION_REQUIRED`。 |
| 修正 → 资源绑定 | 第一、第二 attempt 清单及脚本 | 第一动作声明 `genomics/wgs-short-read-mapping#scripts/inspect_wgs_inputs.py`，登记了四项资源闭包并注入证据目录。`attempt_5b54745070536c0e7c08` 没有该声明/绑定，但 Python 仍访问变量，在 helper 启动前结束。不是执行器从不注入变量。 |
| Generate → 接收 | 六份真实请求的 `current_generate_output_protocol`、四份原始拒绝响应 | 普通、Recovery 后的协议内容原本相同且含 helper 分支；不能归因为 Recovery 少发协议。四份不是同一种格式问题，见下表。 |
| 停止/显示 | `generate-0029-6dbbe010d714/manifest.json`，取消归档，Recovery 0012 原文及三个工具回执 | 请求在 `2026-09-18T12:41:40.239991+00:00` 取消，局部清理为 `CLEANUP_CONFIRMED`；这不是全局清理证明。长英文摘要来自普通 Recovery 模型 content，非压缩器事件。三条“裁剪无续读位置”对应的 stdout/stderr 裁剪标志均为 false。 |

原始候选回放（完整原文及 request/response SHA-256 在 `tests/fixtures/preflight_1789734892_candidates.json.gz`）：

| 候选 | 原始内容情况 | 新接收结果 |
| --- | --- | --- |
| `generate-0016-0e0871769ff5` | 说明文字 + JSON 围栏 | 仍拒绝，指向 `$` 的响应外层；不从说明中抽取代码。 |
| `generate-0026-cf47cfed1b46` | 说明文字 + JSON 围栏，payload 内又是 skill_script JSON | 首先拒绝外层；独立结构用例及真实下一轮纠错验证 `$.payload`、顶层 helper 分支及合法示例。 |
| `generate-0027-bc332adb1cce` | 整段唯一 JSON 围栏，payload 是脚本块 | 可以正常接收载体并进入动作检查；重放在 Generate 后停止，不批准/执行原脚本。 |
| `generate-0028-33292b74715f` | 整段唯一 JSON 围栏，payload 是脚本块 | 同上。通过载体接收不代表原脚本科学正确。 |

## 生产修改

- `result_resolution.py` 复用既有 `result_selections`、`organize_result`、本地 publisher 和登记机制。由当前 attempt 的策略绑定、原 stdout 摘要/解析回执及 checks.json 的明确文件清单提供可选候选，不用目录名称猜正式产物。原 checkpoint 只有父目录观察时也可以定点重建候选。
- 发布前后核对原输入身份、计划/批准身份、原 attempt 状态、证据文件完整摘要。第二 attempt 已替代第一 attempt 时拒绝旧结果冒充当前结果。未知目标目录冲突、符号链接、证据缺失/改变均失败关闭；不删除或覆盖已批准目标。
- 发布程序由现有 Generate 机械组装，经原代码审批和执行入口，保留原文件及嵌入引用。目录发布有该动作专属暂存所有权记录，并在移动前记录目录 device/inode，以识别移动后、完成回执落盘前的中断；同字节但无法证明归属的目标仍拒绝。支持中断续行；不是一个新的任务恢复控制器。核验及 Commit 明确采用重新核验的原计算 attempt，发布 attempt 保持独立身份和核验记录。
- `continue_action_skill_selection` 只在已接纳的同一步 retry、同能力、同资源快照下继续原已选 helper 的 handles；正常 binder 重新解析资源、环境及本次 attempt 绑定。显式新 handles/空 handles、能力变化进入各自正常路径。没有从脚本文本反推 helper，也没有复制旧环境/权限。
- Generate 普通、局部纠错、Recovery 后共用输出协议。完整唯一 JSON 的整段无标签/json 围栏只剥离外层；有外部说明、多对象、多围栏、重复字段或响应未完成继续拒绝。helper 错放 payload 时给字段位置、期望类型、正确分支和当前合法 schema 示例。
- CLI 按裁剪字典中的 true 值判断，不把非空字典误作已裁剪；有真实裁剪时显示日志/分页引用。重复取消提示按当前输入周期去重。请求清理回执明确仅限本次请求；终端结束按现有取消回执、末次请求本地清理和进程登记事实显示，不再固定刷“整体清理未确认”，也不把空登记表称为远端已清理。
- 普通 Recovery 长英文摘要保持普通模型输出的事件来源；补充现有公开文字的简洁中文风格要求，没有新增英文标题或 System 关键词过滤器。内部遥测/维护事件仍走详情。

发布目录 inventory 的限制为当前显式平面清单（最多 257 个文件、32 MiB），不递归扫描项目或原始输入树。超出范围需要现有审阅/正常修正，不能静默少复制文件。科学核验、输入权限、批准一致性及结果真实性检查均保留。

## 200k 来源：并非本次根因

`config/config.py` 中 `BIOMIND_SCHEMA2_APPLICATION_CONTEXT_LIMIT_TOKENS` 的默认总窗口为 200,000；`BioAnalystAgent` 的 `llm_context_limit_tokens` 来自 `max_memory_tokens`，CLI 启动仍传 200,000。Generate 与 Recovery 取两者最小值作为应用总窗口，再结合 provider/model 窗口。

原始请求记录的 `context_capability_source=legacy_application_total_window`，`known_provider_context_limit_tokens=null`，输入软阈值 256,000；输出预留 50,000、估算余量 4,096、安全余量 2,048，因此有效输入为 143,856。Recovery 最大估算为 128,701，所有已核对记录 `hard_block=false`。配置文件和入口解释了该应用上限；归档没有保存能唯一证明当时 shell 覆盖来源的完整环境，不能把默认值推断成当时没有任何覆盖。没有读取受禁止的密钥文件，也没有在本轮更改该窗口。

## 验收与复现

最终 **315 项唯一用例通过**（其中本次针对性用例 64 项），包含修正旧夹具后的回归重跑；最终没有未解决失败。实现提交为 `20654c99c62890dff0c7587e5e287d85071ea740`。验证源代码 tree 为 `d3fe9c068eade916290442aea4bec8322585bfdc`。

以 `test_results.json` 的最终逐项记录、源码签名和 Git tree 为准，`reproduce.sh` 从指定 Git tree 导出允许的源代码/小型归档到 `/tmp`。测试入口禁止网络、原工作区/密钥读取和非白名单进程；不导入实际配置/启动流程。

覆盖实际 Generate 请求及接收、动作材料化、审批对象、执行清单、Supervisor、登记、Commit 和图 checkpoint 续行；覆盖原四份拒绝候选、原五项回执离线重放、同 helper Python/Shell 修正、显式普通脚本、资源快照变化、未知目标/失效输入/过期批准/被替代 attempt、复制中断及核验前中断。审批策略对象与磁盘执行清单逐份比较。两次图续行的 helper 替身累计只有一次，原科学 helper 从未执行。

回归另含既有 256k/累计观察/分页回读、结果选择、公共响应及 CLI 显示测试。旧结果选择测试替身补齐了实际响应完成标志；坏压缩文件用例明确要求内容核验，不再用“只检查存在”的契约暗示内容有效。空文件存在性用例也保留 content_valid=false，并核对 validation_scope=existence_and_kind，避免把存在性通过写成内容验证成功。两个会写磁盘的旧 Recovery projection 夹具也改用 TemporaryDirectory，未扩大隔离器写权限。没有为使测试通过放宽生产接收条件。

## 工作区保护与未覆盖项

通过任务开始前逐文件备份、临时 Git index 和独立导出验证构造提交，没有 reset/stash/覆盖现有内容。提交包含本轮实现及其已有未提交协议/helper 组装依赖；Planner feedback 的既有实现也作为 HEAD 已引用函数的必要依赖纳入。其他 workspace authoring、资源契约、Skill 文档等无关修改继续留在原工作区。文件范围另见 `file_scope.json`。

未验证真实 provider 对新提示词的遵循程度、原会话自动恢复、真实输入新鲜度/科学正确性、完整 WGS、远端任务清理、真实交互终端的人工视觉效果。离线通过不能写成真实恢复或科学任务成功。原历史会话后来已有第二 attempt 且最终取消；本轮没有改写其状态，免重跑仅在第一次核验失败检查点的隔离重放中证明。
