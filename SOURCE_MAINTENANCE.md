# BioMind Studio 本地源码保存边界

2026-10-13 本地成果保全记录。此目录是 Studio 唯一维护根；本轮只增加 Git 元数据、窄范围 `.gitignore` 和本说明，原有业务文件字节不变。提交保存不等于构建、验收、部署或当前 Windows 窗口已加载。

## 以后改哪里

- Windows 客户端：`desktop/`，Electron 连接管理在 `desktop/electron/`，界面和桥在 `desktop/src/`。
- Linux 后端与核心协议适配：`backend/pi-studio/`；重点入口为 `lib/biomind-session-adapter.ts`、`lib/biomind-pipe-launcher.py`。
- 服务生命周期：`server/`；打包配方：`packaging/`；构建/检查脚本与合成测试：`scripts/`、`tests/`。
- BioMind Main、登记、bash、主提示词、压缩提示词和插件的权威源码仍在 `/public/home/liusiqi/03.毕设/BioMind-worktrees/unified-core-plugins-20261009`，使用独立核心 Git 数据库。本 Studio 仓库不替代核心仓库。

保全批次依次保存后端及来源说明、客户端、服务与打包支持；随后补存原清单遗漏的会话 API 路由。批次间是保存顺序，不声称每个中间提交均已独立构建通过。最终提交、父提交、文件哈希及独立备份见 `/public/home/liusiqi/03.毕设/BioMind_Workspace_Preservation_20261013`。

## 来源及历史范围

客户端来自 `https://github.com/flowflic/Pi-Studio` 的 `0fbc8178b80af55bf043465ef36d5bfe8373463c`，本地上游仓库为 `/public/home/liusiqi/03.毕设/BioMind_Studio_Workbench_20261009/upstream/flowflic-pi-studio`。本轮直接读取该 Git 对象逐路径比较；73 份同字节、21 份已适配，另有本地新增文件。现有 `desktop/package.json` 为 `1.3.0-rc.20261009.1`，不能用上游干净提交代替本地适配。

后端历史来源为 `https://github.com/AACS111/pi-studio` 的 `70a6a1a9763d26778fd6def702eeea6994739d56`，之后经历 BioMind RemoteAgent、S1/S2、Workbench 和统一核心适配。当前 package 版本 `1.1.0`，锁定依赖与客户端不同。来源证据为 2026-09-28 的 `BioMind_RemoteAgent_Candidate_20260928/studio_source/ARCHIVE_LOCK.json`，并以现存本地基线核对必要文件。原清单所指 ZIP 目前不存在；本轮没有重新下载，未将历史 archive 校验冒充当前校验。补齐 9 份路由后的 510 份后端文件中，425 份与历史 archive 清单相同、43 份不同，其余为本地新增。

本仓库从当前经审查快照建立新历史，没有导入完整上游 Git 历史。源码扫描覆盖精确候选文件；真实凭据和用户数据目录不在扫描/复制范围。扫描发现的口令值均核对为合成测试、文案或示例；这不是对全磁盘、所有历史或未来新文件的无秘密保证。

## 许可证与资源

客户端 `desktop/LICENSE` 是 Pi Studio contributors 的 MIT 许可；`desktop/THIRD_PARTY_NOTICES.md` 与 `desktop/src/main/vendor/dsh-cost-meter-LICENSE` 原样保留。图标与本地上游 Git 对象一致。后端原有图标与历史 archive 清单一致。`backend/pi-studio/app/fonts/NotoSansMono.ttf` 搭配同目录 `OFL.txt`，按其中的 SIL OFL 1.1 条款保留，不因是二进制资源而丢弃。

后端维护根缺 `LICENSE`，本轮不补写业务路径。下方完整保留本地 2026-09-28 基线 LICENSE 的 MIT 文本；其 SHA256 为 `19cee3b1b8fd9b42f9515366bc1a7dfaaf22fcb006234ebb85ff2f6f8c52eb2b`，与历史 archive 清单一致。第三方依赖有各自许可证；没有对所有传递依赖完成新的发布许可审计，也不新增对 BioMind 自有适配的授权声明。

```text
MIT License

Copyright (c) 2026 agegr

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 保存了源码，但尚未证明完整重建

653 份旧候选不是提交白名单。`server/setup-preset.json` 是真实安装预设，保留原位，本轮不备份、不提交；其余 652 份源码、锁文件、构建配置、合成测试和资源进入前三个保存批次，并加入本说明及 `.gitignore`。收尾发现旧候选清单漏掉 `backend/pi-studio/app/api/sessions/` 的 9 份路由代码；它们是产品源码，不能按目录名误当真实会话数据。逐项核对来源及内容后，补入第四批保存，业务文件字节不改。最终为 661 份原有源码/资源及 2 份新增管理文件，共 663 份 tracked 文件。

当前后端的 `pnpm-workspace.yaml` 和 `pnpm-lock.yaml` 引用两份不存在于本维护根的补丁：

- `patches/@earendil-works__pi-ai.patch`：SHA256 `5876dce7ad25df90231392adccdbf0febe73791966cef080600de03a853ad93f`。
- `patches/@univerjs-pro__uexcli@0.4.1.patch`：SHA256 `17450c6cdca69faec2769d2c079e0abf8757f75c943b48c4a533827cafcf9c7d`。

这两个哈希同时匹配现存锁文件、历史 archive 清单及本地基线文件。相关 npm bin/测试还引用当前缺失的 `bin/pi-studio.js`、`bin/node-version.js`、`bin/pi-studio-options.js`。两份补丁、三个 bin 文件及 LICENSE 已作为历史恢复输入单独私有保存；没有补回本目录、没有纳入当前源码提交，不声称它们是本目录既有文件。恢复材料在 `/public/home/liusiqi/03.毕设/.BioMind-Git-Backups/20261013-preservation/studio/recovery-inputs/`。

`packaging/prepare-windows.py` 还需要当前安装预设、目录外 `../core-candidate.json`、明确核心 release、已有 Windows Electron/NSIS 资源和依赖安装目录。现存 setup preset 与 `core-candidate.json` 的核心引用为 `biomind-2fb23b420cc8d7bd`，只是既有打包输入，不能表示用户当前服务或 Windows 连接。后端包的 Pi 0.84.2 也不能代替实际 BioMind Main 的版本身份。

现有 `node_modules` 是外部依赖链接，`.next-pkg`、`desktop/out` 是生成物；全部原位保留。本轮没有安装、构建、执行产品测试、发模型请求或修改启动配置。完整重建和发布资格均为 NOT_RUN，缺失输入须在未来单独授权的候选工作中处理。

## 运行数据与备份

真实连接配置、`deployment/`、private、凭据、软件登记、sessions、数据库、科研数据、模型轨迹、构建产物与安装包不属于源码 Git。它们可能是运行和回退必需内容，未提交绝不等于可删除。本轮只记录保护要求，未复制真实私有配置/用户数据。

本地 Git 及逐文件副本、限定本仓库 `refs/heads/main` 的独立 bundle 和离线对象恢复核验由本阶段清单记录。备份在同一服务器存储故障域，不是异地备份；无远端配置、未推送。恢复检查目录只是本轮校验材料，不是第二套长期 Studio 维护源码。

以后每次任务结束分别报告：工作文件状态、源码提交、可恢复备份、远端/异地状态、构建身份、该构建验收、生产入口、当前会话加载身份。失败恢复核心 WIP 不自动进入稳定发布，Studio 保存也不批准任何核心 release 切换。
