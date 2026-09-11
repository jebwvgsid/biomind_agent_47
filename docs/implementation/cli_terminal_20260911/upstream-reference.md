# 官方源码参考与固定版本缺口

已取得官方 commit 页面，固定候选 SHA：
`e25bedc166b54acadb505b587a387091097b2393`

来源：https://github.com/openai/codex/commit/e25bedc166b54acadb505b587a387091097b2393

**固定 SHA 下的源码文件尚未取回、核对，故该项未完成。** Git 公共只读访问在沙箱内外均无法连接；固定 raw/blob URL 的网页抓取返回 cache miss；最后一次不读取 curl 配置的直接下载返回 DNS resolution failure。未用动态 main 冒充已验证的固定版本。

实际阅读的是下列官方动态源码及其相关符号。网页缓存时间不同，不能证明它们都对应上述 SHA：

| 文件 | 实际参考职责 | 本地实现 |
|---|---|---|
| `codex-rs/tui/src/lib.rs` | 终端输出边界 | 显式 display_print/emit；不 patch builtins/sys.stdout |
| `history_cell/mod.rs` | 稳定历史与活动内容 | DisplayModel.history 与 active 分开；历史经 owner 追加 |
| `status_indicator_widget.rs` / `StatusIndicatorWidget` | 状态、观测时间和取消提示 | 内存显示状态行，无业务加载 |
| `bottom_pane/chat_composer.rs` / composer 与 paste 文档 | 编辑缓冲、粘贴与提交边界 | prompt_toolkit Buffer；完整文字缓冲；Ctrl+J；不自动排队 |
| `tui/frame_requester.rs` / `FrameRequester`, `FrameScheduler` | 合并绘制请求 | 合并跨线程通知与 Application 0.1 s redraw interval |

实际阅读 URL：

- https://github.com/openai/codex/blob/main/codex-rs/tui/src/bottom_pane/chat_composer.rs
- https://raw.githubusercontent.com/openai/codex/refs/heads/main/codex-rs/tui/src/lib.rs
- https://raw.githubusercontent.com/openai/codex/refs/heads/main/codex-rs/tui/src/history_cell/mod.rs
- https://raw.githubusercontent.com/openai/codex/refs/heads/main/codex-rs/tui/src/status_indicator_widget.rs
- https://raw.githubusercontent.com/openai/codex/refs/heads/main/codex-rs/tui/src/tui/frame_requester.rs

本地 Python 代码独立编写，只参考组织方式，没有复制/翻译 Rust 实现、品牌标识或引入 Codex Agent/RPC。第三方参考代码未纳入 BioMind 工作树。`prompt_toolkit 3.0.52` 使用本机 conda 包缓存的离线副本，在 `/tmp/biomind-cli-deps` 验收，未修改现有 Python 环境。
