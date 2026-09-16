# D4 / C4：生成量、展示和取消边界

本节只核对当前源码、`session_1789521763` 原始记录和已有隔离回执；未修改生产。`test_d4_output.py` 是当前行为见证：其中“标签仍公开”“无短副本限制”等断言通过，表示缺口被复现，并不表示 C4 已修好。真实模型、远端取消/计费、科学 E2E 均为 **NOT_RUN / UNKNOWN**。

## 1. 本次发生了什么

首次失败之后只有一个 Recovery episode。六个完成响应合计约 59,300 字符，第七次请求在尚未观察到 payload chunk 时取消。这些完成轮含普通 `content` 中的 `<analysis>`，并发出 inspection 工具调用；不是 provider 原生 reasoning 字段被 renderer 当作正文。本次简短公开说明提示已存在，但模型没有遵循，程序把调查轮普通正文投影到公开说明。这是**模型真实偏离 + 旧展示修复未覆盖此入口**。不能据 59,300 字符推算 Token，也不能把 511,285 个已报告总 Token 全部归到输出。

本次未发生输出长度/总时限触限，也没有 compaction。因此不能说本次绕过了触限后的拒收、总时限失效或压缩失败；能证明的是：既有每轮时间、总轮数和最终字符检查允许了这次较高成本调查，未在取消前形成决定。

原始锚点（下列路径以本次 run 为根）：

- `context_trace/recovery_turn-0016-c52435e83e2c/request.json`：首轮真实组装的请求含公开说明约束。
- `context_trace/recovery_turn-0016-c52435e83e2c/response.json` 至 `recovery_turn-0021-885358279950/response.json`：六个实际响应。
- `context_trace/recovery_turn-0022-9ede006341a4/manifest.json`：第七轮 `status=cancelled`，温度 0.6；输出上限字段不存在。对应 `response.json` 仅记录 caller cancellation 和 provider attempt ID。
- `context_trace/events.jsonl:152`：provider `CANCELLED / PROVIDER_CALLER_CANCELLED / CLEANUP_CONFIRMED`；0 个 payload callback、`stream_started=false`、`provider_usage={}`、`usage_status=UNKNOWN`。
- `context_trace/events.jsonl:153`：Recovery model turn cancelled；`:154`：独立 Ctrl+C 来源的 task cancellation 已持久化，generation 105→106。

## 2. 参数构造：什么已传入，什么未知

| 层次 | 当前事实 / 本次保存值 | 精确边界 | 能证明到哪里 |
|---|---|---|---|
| 共用客户端 | `streaming=True`，显式 `timeout`、`max_retries`、temperature、extra_body；未构造 `max_tokens/max_completion_tokens/max_output_tokens` | `bio_agents.py:2623` 的 `llm_kwargs` | 证明当前显式构造；不把模型默认最大输出猜成业务限额 |
| Recovery 客户端 | 拷贝共用参数，固定 SDK retry=0、cache=False | `bio_agents.py:1151 recovery_client_kwargs`，`:2680` | 应用层物理重试 owner；并非输出限额 |
| Recovery thinking | 当前 helper 仅在明确启用且识别目标时绑定 thinking/reasoning_effort；本次 manifest 为 disabled，effort 空，temperature=0.6 | `bio_agents.py:1090 _bind_deepseek_thinking_llm`、`:1203 bind_recovery_llm` | 字符串 `<analysis>` 不会因此成为原生 reasoning；不证明远端采纳声明 |
| 本次物理请求时间 | 首轮 first/idle/total 均 599.9709549499676s，cleanup=5s；第七轮均 599.9846251122653s | 首轮/第七轮 manifest `provider_timeout_policy` | 这是请求 policy 记录；尚未触限不能反推远端最大时长 |
| 当前 Recovery 时间构造 | `total=min(legacy_timeout,llm_total_timeout,provider_policy.total_timeout)`；非 Generate 的 first/idle 均设为 total；Recovery 每请求应用最多两物理尝试 | `bio_agents.py:18171`–`:18207` | Recovery 未使用 Generate 独立配置的 first/idle 值，仍有绝对 total deadline |
| 终态摘要 | `terminal_summary_llm.bind(max_tokens=1500)`、30s policy | `bio_agents.py:2690` | 仅终态摘要专用，不是调查轮 Recovery 限额，不能列为本次输出保护 |
| 本地 SDK | `max_tokens` 字段可用；`ChatOpenAI` 把它改名为 `max_completion_tokens`，timeout/max_retries 传入本地 OpenAI client，支持 extra_body/reasoning_effort | 本地 `langchain_openai/chat_models/base.py:571,592,624,627,697,925,1012,2987,2995` | 只静态读安装包源码；未构造真实 client，未请求服务。实际远端支持、最终采纳值 **UNKNOWN** |
| 输出限额遥测 | Generate 使用 `output_policy_projection` 白名单读绑定层；remote limit 与 transport confirmation 固定 UNKNOWN | `agent_core/provider_attempt.py:148`，`bio_agents.py:18165` | 是诊断投影，不施加限制；Recovery 没有对应输出限额投影调用 |

本地 SDK 路径为 `/public/home/liusiqi/anaconda3/envs/biomind/lib/python3.12/site-packages/langchain_openai/chat_models/base.py`。只读取公开 SDK 源码及上述业务字段；没有读取秘密配置、凭据值、完整环境或启动脚本。未从既往模型名推断本次远端能力，也没有网上查服务。SDK 静态源码只证明当前安装包装器可处理这些字段，不是本次已加载 SDK 字节或 HTTP payload 抓包；本次最终 wire 的输出参数值没有确认回执。

**具体缺口**：Recovery 当前没有显式生成量预算进入构造参数，未建立“业务请求值→本地 SDK payload 字段→远端确认”的已证链。应区分“当前未显式请求”与“远端没有任何默认上限”；后者未知。

## 3. 实际通路：五种内容各归其位

```text
Recovery request / provider adapter
  -> run_provider_attempt + ProviderStreamObserver
       原生 reasoning / content / tool fragment 分别观测、计字符与活动
       first / idle / absolute total watchdog；取消 fence + drain
  -> 完整 AIMessage 原始检查点（审计与恢复消息）
  -> RecoveryAgentLoop._content_text / strict reception
       finish_reason / MAX_CHARS / 原始 tool args / schema / scope
  -> 原始业务指令或 pending inspection calls（不能消费展示副本）
  -> _current_public_explanation（另取公开副本）
  -> _schema2_recovery_event_sink
  -> emit_public -> DisplayModel.receive -> safe_text -> CLI
```

- **原生 reasoning**：`ProviderStreamObserver._chunk_fields` (`provider_attempt.py:488`) 单独观测；Recovery 列表形式 content 的公开副本仅取 `text/output_text`，排除 `reasoning` 块（`recovery_agent.py:1270`）。已存在隔离测试 `tests/test_cli_model_recovery_tools.py:182`。
- **普通 content**：`_current_public_explanation` (`recovery_agent.py:798`) 不检查 analysis 标签。有效 inspection 轮会公开普通 content；纯正文且 `RECOVERY_SUBMISSION_MISSING` 也可公开。长度低于字符总检查时，不因没有 directive 免计轮数。
- **原始工具参数**：先保存原始消息，严格解析参数；完整且合法的 native submission 使用 directive.summary 公开，拒收 native 轮只公开状态，不回退公开原正文（`:1289`）。这项旧修复有效，但 inspection 轮不走该分支。
- **公开模型文字**：`bio_agents.py:3443 _schema2_recovery_event_sink` 的 response 分支把 explanation 传入 `emit_public`；`cli_display.py:86` 不识别 analysis，`:284` 的 `safe_text` 仅做终端控制字符/秘密模式处理及超长整串省略。`safe_text` 超过 65,536 字符显示省略，不能减少已经生成或接收的输出成本。
- **执行端 stdout/stderr**：`StreamDisplayCopy` (`cli_display.py:413`) 是执行原始 bytes 的独立展示解码器，缓冲不完整行并过滤机器标记，**不是 Recovery 模型 analysis 过滤器**。不能把它的跨 chunk 行处理当成本次模型正文已受控。

Recovery 当前等待完整 `AIMessage` 后才选择公开副本，并非把其每个 provider chunk 直接公开；本次“刷屏”是大段完整 response 被公开。离线见证把 `<analysis>` 拆在多个 `AIMessageChunk`，合并后送真实 Recovery reception，再送 `emit_public / DisplayModel`：闭合标签、未闭合标签均保留并公开；代码中的字面标签也保留。前两者是缺口，后者是必须保留的兼容行为。此见证不声称当前存在一个模型逐 chunk renderer。

## 4. 已有资源和执行边界，及其局限

### 已有有效边界

1. `run_provider_attempt` (`provider_attempt.py:1124`) 从固定 monotonic 起点计算 total remaining。内容、原生 reasoning、工具参数 chunk 能刷新 idle，不能重置 total。现有 `test_a7_active_stream_still_hits_total_deadline` 和本次 D4 连续 chunk 见证覆盖该共享 owner。
2. `RecoveryAgentLoop` (`recovery_agent.py:1206`) 在派发 inspection 之前拒收 `finish_reason=length` 等非完整结束，`:1209` 检查 128,000 字符。原始消息/解释保留，不把被截断/取消的半段参数修成命令。
3. provider 流中断、超时产生 `partial_payload_disposition=discarded_before_parse`；provider caller cancel 先 fence、drain，再传播，不返回给上游 parser。unknown usage 保持 `{}` + `UNKNOWN`，不是 0。
4. 纯正文会消耗 turn 和格式纠正机会；无 directive 不代表不计预算。工具错误亦计工具调用。D3 负责累计 episode/reentry 的覆盖缺口。

### 仍缺的边界

- 128,000 字符是**完整接收后**检查；没有阻止达到该值之前的持续生成。65,536 字符是展示省略，也非生成预算。
- 普通 content、原生 reasoning、tool args 各有观测计数，但当前 observer 没有本次所需的生成期输出容量拒收策略。
- 当前每请求 total 约 600s，不等于调查 episode 累计时长/累计 token 限额。分页真实前进也不得清零累计资源；详见 D3。
- `finish_reason=UNKNOWN` 被当前 reception 允许。它与已明确 `length/cancelled` 不同；允许 UNKNOWN 不代表已取得远端完整性证明。协议仍必须通过 JSON/schema/scope 检查，缺失 finish 元数据应如实记录。

## 5. 取消、迟到、退出：不能合并三种清理结论

| 观察 | 本次已证 | 不能推出 |
|---|---|---|
| provider local task | events152 `CLEANUP_CONFIRMED`；最后请求0 chunk，usage UNKNOWN | 远端停止生成/计费、工具进程均退出 |
| 持久业务终态 | events154独立用户取消；task/runtime/execution视图均 cancelled | 对所有资源完成清理或新写科学成功交付 |
| CLI | 用户日志776–783显示 provider确认、任务取消已记录、运行调用返回，允许编辑新输入 | 实际 OS exit code、所有 daemon/子进程结束 |
| `/exit` | 用户日志784显示输入；当前 `bio_agents.py:54317` 已在分发前整串 trim 匹配 | 单靠输入一行不能证明本次 OS exit code |

用户日志为 `/public/home/liusiqi/.codex/attachments/7f79322b-e892-48d2-9b44-dfb3e6c9050f/pasted-text.txt`。本次“整体清理未确认”是代码有意保持 scope 的文字（`bio_agents.py:53983,54715`），不能视为 provider drain 未返回。`cli_terminal.py:655` 的重复 Ctrl+C 提示也不是新任务/新模型请求。没有 Ctrl+D 强退原始证据，不推断已执行 `os._exit(130)`。

`context_trace/recovery_transport/recovery-eaa28659b30af706064a0dd9.json` 保留 `status=in_flight`、attempt `dispatched_unknown`。定点核对后，这不能列为已确证的状态缺陷：实际 owner 是 `ContextLineageStore.recovery_transport_claim`（`agent_core/context_lineage.py:702`，无独立 `recovery_transport.py`）。它明确规定未确定请求结果的 claim 跨崩溃保留 UNKNOWN，不恢复免费发送资格；`:719` 对非 `ready/retry_ready` 状态拒绝再次 claim。当前 `bio_agents.py:18515` 的 caller cancel 分支写 `finish_call(status=cancelled)` 和 provider 注释，再传播取消；没有把 journal 改成可重派发状态。成功/确定的 provider failure 分支才分别在 `:18723` / `:18656` 调用 `recovery_transport_finish`。因此本地 drain 已确认与远端结果仍 UNKNOWN 可以同时成立，journal 保留隔离语义也可合法；不能据此断言远端仍运行、重复请求、计费或生产回归。journal 本身没有本地取消明细，但同 attempt 的 events152/manifest已有该证据；需要跨来源呈现时按 ID 关联，不能把未知改成失败或重试资格。整体资源分项与远端清理仍 UNKNOWN。旧 `tests/test_recovery_connection_retry.py:42` 已明确验证 UNKNOWN claim 不会重新获得派发额度，本节只复用其语义证据，不声称本轮重新执行了该旧测试。

### V19 的现有覆盖与诚实限界

- provider取消前 fence、已开始请求 drain、迟到 chunk：本次 witness 使用真实 `run_provider_attempt` 和 observer，替身故意吞取消后返回完整 AIMessage，仍只得到 cancelled，无 completed 接纳。
- Generate await 后迟到合法/非法/blocked结果：复用 `tests/a1_unified_fix/test_f3_late_result.py` 的真实 typed graph / parser / CAS / route；provider和输入准备是替身，未 Execute。不能泛化为所有 Recovery/Commit 入口。
- 任务取消幂等和视图：复用 `tests/phase_b_1789471955/test_control.py:33` 及既有 `docs/implementation/session_1789471955_phase_b_20260915/evidence/control_commands.json`，证明一次 profiler终态、原 attempts/steps 保持、task/runtime/execution 一致；当前 `_commit_cancellation` (`bio_agents.py:25307`) 先查既有 receipt 并保护先前其他终态。
- `/exit` LF/CRLF：同文件 T17 实际 interactive 子进程历史回执，独立命令零模型/零分发，嵌入正文仍按正文。当前源码 `:54317` 保留整串 trim，无旧 `"\n" not in user_input` 前置条件。此处复用，不假称本轮新跑了全部 CLI 子进程。
- compaction取消在规划前/CAS前/CAS后：复用 `tests/phase_b_1789471955/test_integration.py` 对应三变体；D3另做同cause取消重入见证。事件检查点取消会传播。**普通科学 Commit 最后落盘前、每一种工具已就绪待派发窗口的全部交错，本节没有新增完整图证明**，在 V19 组合验收保留必测，不能以当前 provider PASS宣称全链覆盖。

## 6. C4 最小修改设计（阶段 E 待授权）

| 修改位置 | 必要增量 | 保留能力 / 验收 |
|---|---|---|
| Recovery client构造 + `_ainvoke_llm` 已有参数/trace边界 | 使用允许字段配置 Recovery 输出容量；明确本地 SDK实际payload字段并记录requested/resolved/unknown，不照搬terminal_summary的1500 | 容量必须容纳合法工具参数和directive；V11/V12合法大参数成功与length拒收配对 |
| `ProviderStreamObserver` / `run_provider_attempt` | 复用既有字符观测与总deadline，接收D3剩余预算；若流中硬限达到，以明确失败reason走现有fence/drain/丢弃路径 | 字符计数与Token分列，持续chunk不续total，usage未知保留，不能把display省略当终止生成 |
| Recovery公开副本 owner → `emit_public` | 为普通 content 增加明确的有界公开文本合约；不合格时用已有事实状态。若解析标记，采用持久跨chunk状态和代码上下文保护，未闭合部分不提前公开 | 原始response/消息/参数仍供审计与严格parser；代码字面示例不误吞；V18闭合/未闭合/跨chunk/代码四类 |
| `RecoveryAgentLoop` reception和工具派发 + D3现有admission | 保持先完成性/格式/身份/活动状态核验，再派发；新的流限额错误不得通过格式纠正恢复半段执行 | V12 partial工具零派发；V19取消之后完整迟到结果不改变已终止状态 |
| 取消终态与transport journal | 保持provider、task、CLI/OS、remote各自receipt scope；如统一展示，按attempt ID关联已有local取消回执，保留UNKNOWN claim隔离。未证明journal必须改写，不自动纳入生产修复 | 不重复终态写入，不覆盖历史执行事实、不重开派发资格、不承诺远端停费；V19局部确认+整体未知对照 |

不新增第二个摘要模型来缩短每轮公开说明；不建立第二套预算服务，不扫描脚本/环境推导业务指令，也不把展示过滤后的字符串交给执行解析。

## 7. 结论分类与验收状态

| 问题 | 分类 | 状态 |
|---|---|---|
| 原生reasoning列表展示分离、native合法summary/拒收状态投影 | 当前已修；本次inspection普通content入口未覆盖 | 复用旧机制，新增对照见证 |
| 已有简短说明提示下实际长analysis普通content | 模型真实偏离 | 原始响应可证，不能指称内部原生思维泄漏 |
| Recovery生成量未显式请求、流中无输出容量拒收 | 旧修复已生效但未覆盖此入口（Generate仅有诊断投影，terminal_summary是独立绑定） | C4设计缺口，当前行为见证不是FIX PASS |
| 绝对total/length拒收/usage UNKNOWN/provider取消fence | 当前已修的共享保护 | 本次隔离见证 + 旧回执；不能扩大为全链迟到接纳验收 |
| `/exit`换行和取消视图 | 当前已修 | 旧实测回执 + 当前入口核验；本次OS退出码未知 |
| 远端输出limit采纳、取消停费、全局资源清理 | 事实不足 | UNKNOWN；不真实重跑补历史 |
| journal `in_flight/dispatched_unknown` 与local取消 | 当前已有UNKNOWN重放隔离；是否另需journal本地取消字段属事实不足 | 本地与远端scope不同；不是已确证回归，不据此安排生产修复 |

离线执行结果见下节及本目录统一 `verification.json`。当前行为见证与下一阶段目标分别列在 `d4_coverage.tsv`；当前缺口被复现的 PASS 不是修复验收通过。


## 8. run02 实际离线结果

已定点读取 `/tmp/bm_failure_closure_d_run02/results/pytest-results.json`：D4 的 10 个测试函数展开为 **13/13 call-phase PASS**，setup/teardown 亦通过。包含长正文计轮、2种非完整finish拒收、128k接收后拒收、3种标签/字面代码、原生reasoning隔离、连续chunk总时限、unknown usage、取消drain/迟到chunk、输出投影未知、完整迟到响应仍取消。没有改动 witness 来迎合结果。总 run02 其他范围的失败不由这13项覆盖；统一结果保持 root 的完整回执。

真实边界：Recovery reception、严格工具解析与只读dispatcher、DisplayModel/emit_public、ProviderStreamObserver、run_provider_attempt 实际执行。替身仅是模型响应/provider叶子、合成证据资源；没有科学Execute、真实服务或 SDK网络请求。本次新测试未覆盖每一个普通Commit前取消交错，亦未执行全部旧F3/退出子进程；这些限制不因13/13而消失。

导出 manifest `/tmp/bm_failure_closure_d_run02/export/export_manifest.json` 标记 `exact_worktree` / `current_safe_worktree_bytes_with_protected_overlay`；source signature 为 `21b2876e81a4fabda7f2bf0ec3036f2711ed403b8d4b195fffb24df6893e6655`，并明确历史 loaded revision 为 UNKNOWN。它记录的来源字节如下（不是本次真实事故历史加载证明）：

| 来源 | SHA-256 |
|---|---|
| `bio_agents.py` | `ea23e2f03165b8b45bb276d52c752dad3f3fd31ef7ff38623a0935b0dcdc1fd1` |
| `agent_core/provider_attempt.py` | `22673124d88565e2c3331f8ba1345938a8aedf3e0af8e3d14646beca48487bcc` |
| `agent_core/cli_display.py` | `b97c1fc5fc34b0be058db84f20b0170aafcbd95dae22ea888014aa238a81f52d` |
| `agent_core/context_lineage.py` | `2be1761ded59e3220ae91a71e911a1793792c4ba812ea35c7ce8e7033a8d6d79` |
| `agent_core/workflow/recovery_agent.py` | `553b324f2d553e69d3b7770f6ee05bf44db9b6b01f6bd6103b19c1db8648c028` |

本节 witness SHA-256：`14a646c634cfea0cca6f26ce36248ab09a331ab1ea1393080315ab5630b54b10`；完整 run02 pytest 结果 SHA-256：`3436904bd71411d666bb1ccaaa02e56d48c0b24e399f309522eb8b6a777cb900`。共享守卫/模块加载/命令回执由 root 统一保存；本节不把来源 manifest 等同于每个历史进程的 loaded module hash。
