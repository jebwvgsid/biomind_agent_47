# CLI V2 官方交互参考映射

访问日期：2026-09-12。只向官方公开文档/公开仓库请求了通用页面，没有外发本地会话、仓库源码或业务数据。

| 实际查阅来源 | 已确认内容 | 本轮采用范围 |
| --- | --- | --- |
| [Codex TUI styles.md](https://github.com/openai/codex/blob/main/codex-rs/tui/styles.md) | 标题、主要文字、次要文字的层次；偏好终端默认色及有限 ANSI 色 | 简化层次及提示，不把大面积固定区域当主叙事 |
| [Codex history_cell/mod.rs](https://github.com/openai/codex/blob/main/codex-rs/tui/src/history_cell/mod.rs) | 主视图、原始文本及 transcript 各有显示表示；进行中的 cell 可更新；宽度变化需重新计算折行高度 | 主历史与可回看的完整详情分开；当前活动不重复永久刷屏；用真实 PTY 验证宽度变化 |
| [Codex App Server items](https://learn.chatgpt.com/docs/app-server#items) | agentMessage 与 commandExecution 是不同项；公开说明可以标记 commentary；工具完成项具有最终状态、退出码等事实 | 公开模型段落和真实工具事实分开，显示不获得批准或提交权限；没有迁移到 App Server |
| [Claude Code interactive mode](https://code.claude.com/docs/en/interactive-mode) | 可从紧凑工具显示进入详细 transcript；存在专用回看操作 | BioMind 使用现有 owner 的 F2、Ctrl+O、PageUp/Esc，不复制另一产品的所有键位 |
| [Claude Code status line](https://code.claude.com/docs/en/statusline) | 底部状态区用于持续、简短的可扫读信息 | 当前状态占一行；没有采用其外部状态脚本或业务运行机制 |

Codex 公开仓库 commit 页面已实际访问并记录完整版本 `654b0a77d0d2f81aa21f61caf7af4be88fe550bb`：[固定 commit](https://github.com/openai/codex/commit/654b0a77d0d2f81aa21f61caf7af4be88fe550bb)。该 SHA 下的 TUI raw/blob 请求返回缓存缺失；实际读到的是上表 main 页面缓存，不能证明与固定 SHA 字节完全相同。这里只把固定 SHA 作为公开参考检查点，不宣称完成固定版本源码逐字复核或像素一致性。

查阅时 history_cell 已为目录中的 mod.rs；旧 history_cell.rs 地址返回 404。以上映射是具体交互机制的借鉴，不能据此声称 BioMind 已采用 Codex 的运行时、协议实现或全部终端行为。
