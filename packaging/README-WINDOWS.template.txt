BioMind Studio @VERSION@ — Windows x64 候选

请运行本目录中的 BioMind Studio.exe。
便携版需要完整解压整个目录，不能单独复制 exe。
安装版创建“BioMind Studio Workbench”桌面/开始菜单快捷方式。
旧版程序名为 BioMind Remote.exe；打开旧文件不会进入本次新工作台。

首次使用：在软件连接页填写服务器连接与所需服务配置，保存并连接。
工作台提供远端项目、会话、聊天/工具过程、Files 与产物预览，以及服务器设置入口。
自动连接/启动、双会话恢复、真实模型和工具任务仍需在实际 Windows 包上按验收表验证。
本包未执行 Windows 安装/双击验收，不能将源码测试或打包成功等同于产品验收通过。

界面源码：flowflic/Pi-Studio 0.4.6
提交：0fbc8178b80af55bf043465ef36d5bfe8373463c（含 BioMind 远端适配修改）
保留既有 BioMind Main / AACS111 会话适配，不包含 Windows 本地 Pi agent，也未升级 BioMind Pi 引擎。
应用逻辑版本以本文件、BUILD-IDENTITY.json 和 resources/app.asar 身份为准。

此候选保留原 Electron 43.3.0 Windows 运行时二进制。
因此 Windows 文件属性中的产品/版本、exe 图标可能仍显示 Electron；文件名与窗口/界面为 BioMind Studio。
安装器和 BioMind 应用代码未添加 BioMind 数字签名。
桌面连接配置留在 Windows，项目、会话历史、模型、Skills、说明、记忆与执行仍由服务器管理。

未迁移：上游 Pi 1.1.0/Codemode、自动化、桌面远控、自动更新、Skills Hub、扩展包安装更新管理。
所有高级能力以服务器真实 capability 和回执为准；请勿将界面可见等同于功能完整继承。

请使用交付目录中的 WINDOWS-VERIFY.ps1 与 windows-expected.json 核验实际双击文件的 exe/ASAR 哈希。
哈希通过仅证明包身份；实际功能仍需记录 PASS / FAIL / NOT_RUN / BLOCKED 及操作证据。
