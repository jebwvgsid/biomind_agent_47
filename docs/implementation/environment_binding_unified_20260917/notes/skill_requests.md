# 最终 Skill 的真实请求验收

`tests/test_unified_skill_requests.py` 复用真实 mapping graph harness，通过外部 provider 边界提供合成回答，并捕获实际 Generate messages。使用最终 `wgs-short-read-mapping` v1.2.0，模型只返回 `inspect_wgs_inputs.py` 根 handle。真实 binder 展开同一选定快照的四份 helper receipt，真实批准、tiny FASTQ/FASTA helper 读取、结果解析和正式 Commit 保留。

实际请求包含一份白名单 helper catalog，保留参数形状、执行入口、carrier 和显式依赖；没有 raw invocation/interpreter stat 的重复注入。按同一个最终快照及真实 receipts，使用前置 `bio_agents.py` 旧投影逻辑重建 catalog 作为局部对照。独立切片保留在 `evidence/skill_request_metrics.json`；最终累计候选的数值及请求 hash 以 `evidence/final_unified-final-skill-request-metrics.json` 为准。这不是历史完整请求 A/B，也不是 tokenizer 或速度测量。

首轮真实请求发现，仅把阶段读取说明放进 Skill 正文不能保证当前 capability prompt 看见文件名。最终 Skill 在 `capability.execution_guidance` 明确阶段参考文件与读取时机后，实际请求可见 `mapping-methods.md`、`runtime-and-reference.md` 和 `failure-publication.md`，而未常驻这些文件全文。随后使用真实 `SkillAccessService.serve_context_request` 对同一选定 snapshot 完成三份 resource 读取，记录各自内容 hash。该调用发生在请求捕获后，证明实际资源通道可达；不把测试发起的读取冒充模型自主请求或连续模型回合。

实际请求中的 dynamic runtime header 和 raw `script_invocations:` 均为零，当前调用没有这些 header 重复。请求体比文件字节更能反映最终生产上下文，因此不以 Skill 文件压缩比例替代此验收。

同文件补验 Generate 原始完整 JSON 安装工单、正文中夹带 JSON、添加执行字段三类输入。完整工单只产生 typed `installation_requested` 并路由 Installer，其他两类维持当前 Generate correction 路径。验收停在安装接管前，要求零科学执行、零 Commit、无 pending scientific action；不执行安装、不代替 Installer 的目标选择与具体命令批准。

这三项实际 graph 验证发现了 Generate 条件边漏列 `install_subagent` 的接线问题；修正 runtime/production 两处边集合后，**3 passed**，逐项结果与实际 provider 请求见 `evidence/generate_install_work_order_results.json` 及对应回执。此前最终 Skill 的独立实际请求验收为 **1 passed**。
