# Studio v1.2 本地发布配方

2026-10-13 用户授权旧改动整合并接入 v1.2。本轮来源是原位 Studio Git 根；此前保全基线为 `b8efd4a7ef236adc72da6bb07ab82dfb82aa70f0`。未推送或公开发布。精确提交、构建、产物、服务端接入和剩余 Windows 限制，见 `../delivery/v1.2.0-20261013/` 的本轮回执。

## 与旧候选的关系

客户端产品版本为 `1.2.0`，同步 `desktop/package.json` 及锁文件的根版本字段。旧 `1.3.0-rc.20261009.1` 是此前候选编号；本轮按用户产品版本重新命名，数字顺序不能表示功能新旧。现有实际入口 `electron/remote-main.cjs` 不加载上游自动更新器，更新 IPC 明确禁用；安装器也没有比较这些 SemVer 字段，因此本轮不经过自动更新降级路径。appId、`BioMind Studio` userData 路径和用户保存连接格式不变。若今后启用自动更新，必须另行处理版本序列和迁移。

后端 npm 版本 `1.1.0` 仍是 AACS 源码组件版本；其依赖 Pi0.84.2 与服务实际加载的 BioMind Main 是不同身份。不能用客户端版本推断正在运行的核心。

## 补齐构建输入

从已独立保全、且与历史固定 `70a6a1a9763d26778fd6def702eeea6994739d56` 清单一致的字节补齐后端 `LICENSE`、`bin/{pi-studio,node-version,pi-studio-options}.js` 及 `patches/` 两份补丁。补丁 SHA 与当前 pnpm 锁文件一致。只补源码，没有安装或修改共享 node_modules。

## 新产物必须使用独立输出

Desktop 可在 `desktop/` 中调用既有 electron-vite `build --outDir ABSOLUTE_NEW_OUTPUT`，得到 `main/`、`preload/`、`renderer/`。不得覆盖既有 `desktop/out`。无后端运行逻辑改动时，可按完整文件清单和编译适配兼容性证据复用现有 `.next-pkg`，注明原构建日期与 BUILD_ID；若运行逻辑改动，再使用独立 `PI_WEB_DIST_DIR` 重建，不能冒称旧产物由新提交刚刚编译。

`packaging/prepare-windows.py` 要求所有输入显式给出：`--stage`、`--evidence`、`--desktop-build`、`--core-release`、`--setup-preset`、`--studio-commit`。它不再读取旧 `../core-candidate.json`、原位真实 `server/setup-preset.json` 或旧 `desktop/out`。新预设只能有非认证连接/安装元数据，不能含密码、密钥或令牌。若设置了 `setup.mainRoot`，必须匹配明确核心 release。已有 stage/receipt 拒绝覆盖；Git 根可用时还核对给定源码提交。

之后使用 `assemble-windows-dir.cjs` 的 `--stage/--output/--runtime/--evidence` 参数离线组装；任何现有输出目录都拒绝替换、移动或改名。使用 `verify-windows-dir.cjs` 核对 manifest、ASAR 和完整 Electron runtime，再调用 `build-installer.py` 的 native Linux NSIS 配方及 `make-portable.py`。全部输出写入本轮新交付目录。不要运行旧的固定路径 `run-windows-dir.py` 来覆盖历史构建。

Linux 文件/安装器归档校验不代表 Windows 安装、卸载、当前窗口连接或真实运行通过。当前服务端接入由主发布流程单独记录；客户端离线产物不会替用户关闭、重连或更新 Windows 窗口。核心恢复 WIP 的已知失败与用户接受范围仍须跟随核心发布说明，不转写为新语义验收 PASS。

## 已停止服务的维护工具

`server/rebind-managed-services.py` 默认只检查；只有显式 `--apply` 才修改指定服务的非认证元数据。它要求配置/服务/后端三把独占锁、持久状态为 stopped、固定核心与构建清单对应，并按精确原字节保留私有回滚副本。认证文件只检查路径、权限和归属，绝不读取；不会 ensure、启动、停止或发送信号。活动服务应在后续有条件维护窗口处理，不得强制通过检查。

离线测试运行 `python3 -B tests/test_rebind_managed_services.py`；只在系统临时目录创建并清理自己的合成夹具，不接触真实服务。生产实际应用与回滚位置只写本轮交付回执，不能从源码存在推断已应用。
