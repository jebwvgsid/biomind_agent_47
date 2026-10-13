# 失败恢复候选 WIP：保留失败状态

本增量相对 f7f9a603aa4f6b862ed955971953252b76a23668 的 Main 文件，仅为 COMMON_RULES 模板末尾追加 BIOMIND_FAILURE_RECOVERY_V1 正文；基线闭合行被替换，总计206增1删。压缩 v2 由父提交单独保存，未夹入本提交差异。

源文件 integrations/pi_runtime/extensions/biomind.ts SHA256: d44c424a228452f20699afdab5c9ef4d204cbebb134285b3d3e9dd8ccae1efc4。

2026-10-13 Failure_Recovery 已有候选验证：接入 PASS；行为稳定性 FAIL（换名场景出现第4次目标执行）；报告真实性 FAIL（已有日志却声称不存在）。提示词不是任意执行的硬限次机制。strict 先前通过的测试不能转移给叠加本恢复段后的整个候选构建。

本轮只保存已有字节，不修复正文，不运行产品测试或模型，不将其标为稳定版，不自动合并集成/稳定分支，也不纳入生产发布批准。当前默认/S2/CORE30ae/c085没有这段新增正文；存在 dirty 候选构建不等于已上线。

来源为统一开发 worktree，另有 BioMind_Failure_Recovery_20261013/candidate-delta 的同字节快照。私有独立字节备份及本地提交记录见本阶段交付。远端/异地备份、产品重新验收、部署均未执行。
