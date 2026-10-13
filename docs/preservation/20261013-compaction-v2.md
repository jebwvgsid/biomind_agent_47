# 压缩 v2 本地保全（不是发布验收）

基线 f7f9a603aa4f6b862ed955971953252b76a23668。2026-10-13 从统一开发工作区保存未提交字节，未修改提示词正文。

本提交仅包含 integrations/pi_runtime/prompts/compaction-bioinformatics-v1.txt 和 testing/compaction-addendum-pty.py 以及本摘要。文件名 v1 延续加载契约，正文为 v2。

正文 SHA256: 1e8bb19286faf79e099f0232b3d4980ec5b1f3b383ed602550152829912fc19e
PTY 文件 SHA256: a022d45cef8451d38932405983abcde5e7f0ed48d971f7fe4ebd41c0b287ed00

历史证据：10/11 合成 SDK/PTY 接线验证通过；真实模型语义 PARTIAL，仍有遗漏分析时期 T1、添加未授权步骤的问题。10/13 c085 固定候选有合成 provider 请求全文接线证据。这里没有重新执行这些测试，也不把历史结果升级为全部通过。

这次仅保存本地版本。未推送、未构建、未重新验收、未修改运行入口。登记/bash 7af357ec、strict f7f9a603 已在祖先中，不重复提交。

preserve/core-wip-20261013 是临时成果保全线，不是新的产品主线。对应 worktree 仅物化本轮3个已审源码与摘要；其余继承条目在该 worktree 的独立 index 中标 skip-worktree，未检出历史数据。不要用这个局部工作区构建/运行产品，也不要批量暂存继承文件。原统一与默认工作区的业务字节和 index 保持原样。

完整来源、私有备份、恢复前提及状态见 BioMind_Workspace_Preservation_20261013。本提交不含失败恢复 WIP；后续恢复保存必须独立审查，不自动进入稳定发布。
