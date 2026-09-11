# 本轮独立源码复核记录

来源矩阵基于 CLI-2 提交 `1818f3e1c8cfe30d29b570aede25663734d7c152` 之后、CLI-3 验收候选已冻结时的工作区安全源码。矩阵中的 `文件:行号` 指该候选内容；图内闭包采用 `BioAnalystAgent._build_graph.<节点函数>` 简写。实际验收命令、结果与最终提交哈希由本轮总报告记录。本记录未读取原 session、业务数据库或敏感配置。

复核发现并已修复的新 envelope 早返问题：仅验证 `payload` 是非空字符串，会允许其中的 Skill JSON 在非候选分支提前返回，或使 `<solution>` 在脚本解析失败后触发旧终态标签分支。当前 `agent_core/public_response.py:108` 的 `project_generate_response` 在解包新 `biomind_generate_display_v1` 后，先调用原 `parse_executable_script_block` 验证唯一 payload；失败仍保留已识别的业务 payload 作为诊断候选，避免候选身份随公开文案变化。外层损坏/冲突则保留原响应。`bio_agents.py:42800` 的早缓存解析和 `bio_agents.py:43270` 的主解析共用 projection；有错误时不进入 Skill、tag fallback、blocked 或 final solution，继续原 ScriptBlockError/representation rejection 路径。脚本字节、后续语法检查、审批和执行契约继续由原业务函数处理。

完整性缓存变更限于两项字符串元数据：`finish_reason` 和 `completion_integrity`。`provider_response_metadata` 优先采用当前可信 attempt 投影，再采用 SDK response metadata，不读取模型 JSON 中的同名字段；候选缓存读写只保留非空、长度不超过 80 的字符串。`candidate_repair.py` 只给原 content 对象增加可选元数据并在缓存领取时返回它；没有变更 claim/CAS/status/noncandidate 逻辑。Recovery 复用既有 `parsed_result.response_metadata`，重建 `AIMessage` 后仍进入相同完整性检查，没有修改 Recovery context、history、seed、admission 或业务预算。`agent_core/workflow/scoped_context.py:116` 的 `provider_visible_payload` 原本就排除 response metadata。原响应 content、provider digest 和 source-message 关联不以显示副本替换。

仍需明确的限制：

- 缺失或 `UNKNOWN` 的旧缓存元数据不能证明响应完整。本轮拒绝明确的 length/max_tokens/content_filter/truncated/interrupted/incomplete 等事实，未追溯补写旧业务缓存，也未新增模型调用来判断未知完整性。
- `agent_core/recovery.py:5837` 既有 `_clip(raw_response, 64_000)` 保持原样。已有 raw digest 检查可能发现内容不一致，但本轮没有解决超限原始响应在该缓存中的裁剪问题，不能宣称任意长度缓存均完整。
- 50ms 显示确认只约束显示等待。正常路径需以真实 writer 的 write/flush 时间证明先于 dispatch；超时、关闭或不可用时业务继续，因此降级路径不能宣称严格先显示后执行。
- 工具返回、Skill 提案返回、命令 `rc=0` 均不是科学成功或任务完成证明。Supervisor、ValidationEvidence、CompletionGate、原路由和授权继续掌握正式状态。
- `safe_text` 和流副本是有限脱敏/控制字符过滤规则，长行及机器协议行可能省略；不保证识别任意未标识秘密，也不替代完整日志或正式证据。

本次独立复核没有对生产入口发起真实模型/API 请求、科学任务、原 session 恢复或原业务状态读取。矩阵包含未新增 public_message 的 Supervisor/final 以及未逐条打印 activity 的 plain 行为，避免把协议接入范围或显示能力扩大表述。
