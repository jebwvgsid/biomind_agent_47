# 实际验证与交付限制

日期：2026-09-14。只测试新增独立原型及接口草案；没有 import BioMind、读取真实会话数据、调用模型或启动生信命令。已确认无生产运行时代码变更，因此不启动生产集成测试。

## 已执行且通过

命令工作目录为 `apps/remote-workbench`。完整服务器 Node/TS 路径见该目录 README；下列 `NODE`/`TS` 仅是表内缩写，不是默认 PATH 中已经可用的命令。

| 实际命令 | 结果 |
| --- | --- |
| `BIOMIND_TYPESCRIPT_PATH=<TS 5.9.3 库> <Node 22.18.0> scripts/build.mjs` | TypeScript strict 类型检查 6 个源码文件通过；生成自包含 `dist/index.html`，93,328 字节。无外部运行时资源。 |
| `<Node> tests/service.test.mjs` | **16 tests / 16 pass / 0 fail / 0 skipped**；Node 自带 test runner 的实际逐项 TAP 输出。 |
| `<Node> --test tests/service.test.mjs`（早期 15 项版本） | 退出 0；该受限环境包装输出按测试文件汇总为 1。最终使用上一行直接运行取得 16 项逐项结果，未把“1 文件”误写成 16 项实测。 |
| `<Node> --check` 分别检查 `scripts/build.mjs`、`serve.mjs`、`export-preview.mjs`、`browser-fixture.mjs`、`tests/browser-smoke.js` | 5 个脚本语法检查通过。 |
| TypeScript API `createProgram(['protocol.draft.ts'], {strict:true,noEmit:true,target:ES2022,lib:[ES2022,DOM]})` 并检查 `getPreEmitDiagnostics` | 接口草案类型检查通过；不代表真实服务实现或符合接口。 |
| `<Node> scripts/export-preview.mjs` | 已导出随源码提交的 `preview/BioMind-Workbench.html`。最终检查它与 dist 字节完全一致。 |
| `<Node> scripts/browser-fixture.mjs` | 已生成 `dist/browser-test.html`。这里只证明测试页面可生成，**未运行其中的 DOM 测试**。 |
| 基线 SHA-256、Git status/index 核对 | 既有 69 项 tracked 修改/删除的文件指纹未变；既有索引保留；只暂存新增两个目录。最终结果见 `source-evidence.json` 及最终提交报告。 |

行为测试覆盖：8 个场景和 4 类详情可渲染；中文空格/多行/特殊前缀保留与 HTML 转义；计划与脚本分别审批；重复请求、重复批准、过期版本与身份拒绝；拒绝后重提；回执查询与 ID 冲突；繁忙消息进入演示队列而不改脚本；断线冻结、后台推进、同 run 重连和空 tail；断线命令拒绝；失败恢复后新审批；请求停止与停止确认；资源作用域与待生成产物禁用；长日志窗口与未知观察事件；快照不可变；离线文件包含全部资源且禁止连接；观察者错误不阻断回执与其它观察者。

这些是执行过的服务行为与视图字符串测试，**不是浏览器点击、CSS 几何、屏幕阅读器或 Windows 实机测试**。

暂存前状态校验另发现 3 项并行新增的无关 untracked 入口，均保留，未读取其内容、未暂存。初次严格状态差集断言因此中止；核对确认没有旧条目消失、69 项旧文件指纹不变、HEAD 与索引未变后，按用户授权继续本轮独立提交。清单记录在 `source-evidence.json`。

## 尝试但未成功

| 检查 | 实际情况 / 限制 |
| --- | --- |
| 本地 HTTP 预览 | `node scripts/serve.mjs` 绑定 `127.0.0.1:18765` 返回 `EPERM`。经工具授权范围重试仍被执行环境拒绝，未启动成功；不提供虚构的在线 URL。 |
| Firefox 截图 | 系统 Firefox 102.10.0 的 `--headless --screenshot`，使用本轮临时 profile，出现 EGL/SWGL framebuffer/IPC 环境错误，未产生截图。软件渲染重试限时 35 秒仍未完成。 |
| Xvfb 浏览器布局 | `timeout 35 xvfb-run -a firefox ... --screenshot` 返回 124；Xvfb 无法绑定监听 socket。没有安装系统图形依赖，没有继续无限重试。 |
| Codex 本地浏览器打开服务器 file URL | `open_in_codex` 返回 “Remote file URLs cannot be opened in the local browser.”；因此提供可复制的离线单文件和对话内预览。 |
| npm/前端依赖联网安装条件 | 默认 PATH 无 npm/React 构建环境，shell 外网 DNS/socket 访问失败；未安装系统包。使用既有 Node/TS 产出独立原型。 |

以上是环境/工具失败，不是自动审批审查拒绝，也不是测试通过。**本轮没有可交付的真实浏览器截图**。对话内嵌的是由同一 bundle 生成的交互预览，不冒称测试截图。预览为适应对话容器固定 840px 高度；交付 HTML 使用浏览器视口高度。

## 未验证与复验方式

- 浏览器 DOM 自动测试已写好，运行 `npm run test:browser:prepare` 后在目标浏览器打开 `dist/browser-test.html`，页面显示 PASS/FAIL；建议 1440×900、1024×768、390×844 分别执行。它检查实际会话切换/草稿、多行发送、合成 IME 保护、审批/展开、断线重连、日志、产物、恢复、停止、服务器切换及当前视口溢出。
- 布局截图、深浅主题对比度、键盘全流程、焦点回到触发按钮、200% 缩放和屏幕阅读器仍需手工浏览器检查。
- Windows 中文输入法候选确认、撤销、粘贴、CRLF、多语言组合输入、高 DPI、原生快捷键未验证。源码中的 composition guard 和 Unicode 测试不能代替这些验收。
- Tauri 2/Rust、React 迁移、Windows MSVC/WebView2 构建、安装包、签名、自动更新均未实施/未验收。
- SSH 握手、主机密钥、跳板机、代理、应用鉴权、真实后台存活、CLI/GUI 互斥、真实审批持久性、真实事件补齐/背压、模型流、取消进程树/调度器任务、真实文件 ACL/预览/下载都未验证。原型模拟不能证明后端支持。
- 原型同步内存服务没有持久化队列和网络超时；`demo-*` 身份不是密码学绑定，内存订阅不承担生产有界队列。正式接入所需的具体边界见 `INTEGRATION.md`。

## Git 交付

BASE HEAD：`d1f3779407e891d6fadf1778cd8e0ce7804b1630`。本轮只允许提交 `apps/remote-workbench/**` 与 `docs/implementation/remote_workbench_20260914/**`。生成目录 `.build`、`dist`、`node_modules` 忽略；离线可交付 HTML 在 `preview/` 中明确纳入提交。没有 push。

完整最终 HEAD 与本轮提交 hash 由提交完成后的 `git rev-parse HEAD` / `git log --format='%H %s' <BASE>..HEAD` 取得，在最终答复中报告，避免让一个提交文件包含自身不可能预知的 hash。
