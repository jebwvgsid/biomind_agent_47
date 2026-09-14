# 参考版本与源码复用

2026-09-14 实际查看了 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) 的仓库页面、目录、组件和样式源码，以及 [产品介绍页](https://deepseek.com/harness/)。本轮未能启动其浏览器页面，因此不声称完成了像素级视觉对照。

固定参考提交：`c291e7961a515f6d7af9304e7fd1d257929aef26`（2026-09-10，release 0.1.5 同步记录）。这是本次核验到的参考提交，不保证是当前最新 HEAD；其桌面 package.json 版本为 `0.1.5-rc.2`。

| 实际读取的源码 | 核验方式 | 本轮取舍 / 代码复用 |
| --- | --- | --- |
| [AppFrame.tsx](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/packages/client/ui-layout/src/client/AppFrame.tsx) | 固定提交 | 参考三列布局、中心列可缩和侧栏收起；BioMind 独立实现。原组件依赖 slot/store runtime，不直接拷贝。 |
| [base.css](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/packages/client/ui-theme/src/styles/base.css) | 固定提交 | 查看 CJK 字体处理；本原型独立定义 Windows 中文字体栈、颜色与尺寸。 |
| [ui-conversation apply.ts](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/packages/client/ui-conversation/src/client/apply.ts) | 固定提交，读取装配及依赖代码 | 依赖 Cordis、sessions、slots、fileUpload、workspace 等；不引入其插件装配或 AgentLoop。 |
| [apps/desktop/package.json](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/apps/desktop/package.json) | 固定提交 | 确认 Electron 桌面栈。其桌面运行时包含上游本地能力，不作为本产品远端 BioMind 执行链路。 |
| [AppFrame.module.css](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-layout/src/client/AppFrame.module.css) | master 动态页面，固定版本页面缓存未命中 | 查看 grid、min-width、滚动和侧栏边界；仅补充参考，不能声称此读取对应上述提交。 |
| [ApprovalPanel.tsx](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-approval/src/client/ApprovalPanel.tsx) | master 动态页面 | 查看待审批展示、决定后禁用按钮及失败恢复；本原型自行实现两类审批和版本检查。 |
| [connection.ts](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/connection/src/client/connection.ts) | master 动态页面 | 查看 generation、订阅就绪顺序与观察者异常隔离。只记录设计参考，不复用 ConnectionController。 |

**代码复用结论：未复制上游组件、CSS、图标、标识或运行时代码；只参考会话工作台的组织与交互方式。** 本轮没有把 BioMind 迁移到 DeepSeek 的 Controller、AgentLoop 或插件体系。

上游 [LICENSE](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/LICENSE) 为 MIT，版权主体 `Copyright (c) 2026 DeepSeek`。后续若实际复制其代码或实质部分，必须随对应分发保留完整版权声明与 MIT 许可文本。当前原型无该代码，因此这里记录来源而不宣称上游为本项目背书。

开发时使用的 TypeScript 5.9.3 由 Microsoft 发布，许可 Apache-2.0；它仅用于构建，不内嵌到原型运行时。当前环境复用已安装的编译器，没有把服务器上的 Node、TypeScript、Firefox 二进制或第三方包复制进仓库。
