# BioMind 远程工作台 · 独立交互原型

本目录是一份可完全离线运行的中文前端原型，所有连接、审批、脚本、日志和结果均为**演示模式**。页面不调用模型、不建立 SSH、不读取真实文件、不执行脚本。刷新页面会恢复演示数据，当前会话、草稿与队列仅在页面内存中。

## 立即查看

打开已经随源码提交的 [`preview/BioMind-Workbench.html`](preview/BioMind-Workbench.html)。这是单文件应用，CSS、JavaScript、图标和演示数据全部内嵌，不需要 Node、网络或本地服务。

代码实际位于 Linux 服务器：

```text
/public/home/liusiqi/03.毕设/Biomind_v1/apps/remote-workbench
```

在 Windows 上查看时，将 `preview/BioMind-Workbench.html` 复制到 Windows 本地目录，再用 Edge/Chrome/Firefox 打开。上述 `/public/...` 是服务器路径，不能直接粘贴为 Windows 本地路径。单文件是浏览器原型，不是已经验收的 Windows 安装包。

## 构建与测试

在有 Node.js 22+ 和 npm 的开发机上：

```sh
cd <你的本地仓库>/apps/remote-workbench
npm install
npm run typecheck
npm test
npm run export
npm run preview
```

最后一个命令在本机监听 `127.0.0.1:18765`，浏览器访问 `http://127.0.0.1:18765/`。`npm run dev` 是同一个静态预览命令，修改源码后需要重新构建并刷新。只发布生成的单文件，不暴露仓库目录。可用 `BIOMIND_PREVIEW_PORT` 改端口。

**本次执行环境不允许监听端口**，上述 HTTP 预览没有在本轮成功启动；交付的准确预览途径是离线 HTML 和对话内预览。不存在已运行的服务器预览 URL。

本轮利用服务器已安装的 Node 和 TypeScript 实际构建，无系统安装。复现命令（Bash）：

```sh
cd /public/home/liusiqi/03.毕设/Biomind_v1/apps/remote-workbench
BIOMIND_NODE=/public/home/liusiqi/.trae-cn-server/bin/stable-54444fce4b47dc54f509b370c7da0d9fe815fda3-debian10/node
export BIOMIND_TYPESCRIPT_PATH=/public/home/liusiqi/.trae-cn-server/bin/stable-54444fce4b47dc54f509b370c7da0d9fe815fda3-debian10/extensions/node_modules/typescript/lib/typescript.js
"$BIOMIND_NODE" scripts/build.mjs --check
"$BIOMIND_NODE" scripts/build.mjs
"$BIOMIND_NODE" tests/service.test.mjs
"$BIOMIND_NODE" scripts/export-preview.mjs
```

浏览器 DOM 测试另行准备：`npm run test:browser:prepare`，然后打开 `dist/browser-test.html`。该页面会操作实际 UI 并显示逐项结果。**生成测试页面不等于执行测试**；本轮浏览器环境失败，该测试未执行。建议分别用 1440×900、1024×768、390×844 视口运行，再手工检查布局、焦点与缩放。合成 composition 事件也不能替代 Windows 中文输入法实测。

## 可以操作什么

- 左侧切换两台演示服务器、三个工作区、历史会话；搜索会话、新建会话，切换时保留草稿和引用。
- 中央发送中文多行文本，Enter 发送、Shift+Enter 换行；`/stop` 和 `!` 只是文本，不解释成命令。运行中提交进入明确标注的演示队列，不自动派发。
- 展开计划与完整脚本，分别批准或拒绝。版本和审批身份过期时拒绝；重复请求不会重复模拟执行。
- 顶部“体验场景”覆盖未连接、新会话、运行中、计划审批、脚本审批、失败恢复、断线重连、完成结果。模拟连接始终写作“演示连接”。
- 点击“推进演示”手动收到工具回执；“模拟失败”进入恢复，更新脚本后重新审批；停止需要“请求停止”和“模拟停止回执”两步。
- 断线保留最后确认状态和草稿；可“模拟后台继续推进”，重连后查看同一运行的进展，不创建新运行。
- 右侧可收起，提供计划、脚本、日志、文件四个标签。日志默认最近 60 条，可生成 240 条长日志并分段加载；未知观察事件仍可显示。
- 输入文件选择只添加演示引用；预期产物不可预览；已生成未核验与已核验结果分别显示。报告预览使用合成指标，下载的是 `.demo.txt` 演示摘要。支持深浅主题和窄屏折叠导航。

## 实现与下一阶段边界

优先评估后，正式产品建议 **Tauri 2 + React + TypeScript**，由 Windows 系统 OpenSSH 连接服务器的 BioMind 薄服务。当前 Linux 环境没有可用的 npm/React/Tauri 构建依赖，外网下载和监听受到执行环境限制。为了交付能够独立打开的实际应用，本轮使用 **TypeScript + 原生 DOM 组件**，运行时零依赖。没有把原生 DOM 冒称为 React，也没有生成未经构建的 Tauri 壳。后续需把视图改写为 React；模型、演示数据、服务行为与测试可以保留。

```text
src/demo/data.ts              合成数据和演示场景
src/types.ts                  页面数据类型和演示服务端口
src/services/demo-service.ts  内存命令、审批、事件和断线模拟
src/ui/components.ts          视图组件（纯数据输入）
src/ui/icons.ts               原创 BioMind 标识及线形图标
src/main.ts                   页面事件、草稿、焦点、对话框
src/styles.css                桌面布局、主题、响应式
scripts/                     类型检查、离线构建、静态预览
tests/                       实际运行的行为测试及待运行 DOM 测试
preview/                     可直接打开的生成物（随源码提交）
```

`WorkbenchService` 是**本地同步演示端口**，不是已经存在的服务器 API。真实接入需要异步命令、缓存快照、请求状态与订阅管理，见 [接口草案](../../docs/implementation/remote_workbench_20260914/protocol.draft.ts)。不会增加模型编排框架。`DemoWorkbenchService` 的 digest、事件游标、执行计数仅用于演示约束，不能用作生产鉴权或完整的审批身份。

详情见 [接入审计](../../docs/implementation/remote_workbench_20260914/AUDIT.md)、[架构与最小修改清单](../../docs/implementation/remote_workbench_20260914/INTEGRATION.md)、[测试记录](../../docs/implementation/remote_workbench_20260914/TESTING.md) 和 [参考及复用说明](THIRD_PARTY_NOTICES.md)。
