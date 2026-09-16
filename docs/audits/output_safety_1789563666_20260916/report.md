# BioMind 文件保护与失败收尾增量修复

## 修改范围审查

本轮以实际启动目录 `Biomind_v1` 的 HEAD `8a84fea91d42f8f036d66badef36b13aec6203f8` 和既有 overlay 为原始基线，复用隔离候选生产提交 `c5ab427fa6865a11bf1f5a2d2c81ca7bcdd53003`、审计提交 `443ed8cd26b1362f2f553ec6a1ee7cafceb0e992`。用户本轮明确选择同步到实际启动目录。未重新审计全仓、恢复原任务或修改科学数据。

定点证据：`session_1789563666` 的注释批准脚本采用 mktemp → snpEff ann → mv；历史执行前后快照和 rc0 证明本次重新生成并替换了旧 VCF。统计脚本在证据排放中重复登记 `ann_summary` 与 `output.ann_summary`，随后 Recovery 缺定位，两轮无进展停止。历史科学事实及旧归档未改写。

原候选仍有三个缺口：静态可确定的排放身份错误直到运行时才拒绝；未发布输出 head 为 expected，现有 locator 仍拒绝；紧凑终态仍偏向错误码且遗漏 `RECOVERY_NO_PROGRESS_LIMIT` 的零总结调用条件。本轮仅补上述缺口和本次授权的输出保护。

## 实际实现

| 边界 | 修改 | 保持的语义 |
| --- | --- | --- |
| Consult / Plan / Generate 提示词 | 明令禁止未经明确授权覆盖、删除、原地改写已有原文件，临时文件再 mv 也不例外；要求 Consult 用既有工具有限检查输出冲突并交接；保留用户必需文件名和路径；缺文件先诊断、不可静默跳过必需输出 | TaskSpec 必需性、Plan 审批、既有工具与交接入口 |
| Generate 准入 | 已知 Python helper carrier 上只检查可证明的字面量排放身份，错误进入原局部纠正；明确资源引用与排放 identity 的区别 | 不运行候选做预检，不推断动态 Python / 任意 Bash 语义，原 runtime serializer 继续校验 |
| 文件写入审批 | 绑定时记录当前步骤已物化声明文件（含 optional）的路径和元数据，纳入 execution policy manifest 与审批 hash；主屏列出冲突目标和备份位置；已有文件必须选择 `backup` 才允许备份后替换 | 没有增加业务 Controller；既有审批对象、完整代码、批准身份核对保留 |
| 误按 Enter / y | 对冲突目标继续等待同一个代码审批决定，不调用 Generate、不启动命令 | 其他审批语义保留，拒绝/修改方案仍走原入口 |
| 执行前保护 | 复核目标状态；明确批准后独立复制旧内容到独占备份文件并 fsync，保存与 attempt/approval 绑定的回执；目录通过核对后的 dir_fd 访问，拒绝符号链接漂移；失败不启动计算 | 不改 login shell、工具环境、科学脚本正文或原有工作区变更验收 |
| Recovery 未发布文件 | 依据当前失败 attempt 的批准路径、真实执行与作用范围允许诊断读；返回 `diagnostic_only/source_uncertain/formal_consumption_allowed=false` | 不产生 valid head、不冒领旧文件、不直接用于下游成功消费；读取时重新核对 scope |
| 停止显示 | 进度、当前步骤、直接原因、实际停止原因和下一步；同 attempt 的完整结构化错误才给出具体身份诊断；未知明确标明尚未定位 | 科学验收、Commit、冻结归档、晚到交付更新不变 |
| 停止成本 | 连续无进展达到上限时跳过额外总结模型，使用有来源的系统事实说明 | 原停止预算及其默认启用状态不变 |

文件保护使用独立的 output_guard_contracts 投影；optional 不会被改成 required，也不改变 controller_output_bindings 的正式消费含义。文件保护还接入保留的 renderer manifest / 已登记 action 结果路径；未通过解析脚本猜测其输出。

## 重要边界

- 输出保护覆盖已有正式声明/清单中的文件路径，是审批与执行前保护，不是操作系统文件访问沙箱。不能声称它能拦住任意代码对未声明路径的全部写入，或阻止另一独立进程在计算期间写同一目标。提示词要求新文件使用不覆盖写法，任务目录应隔离；本轮没有迁移执行内核或新增全局语义分析器。
- Consult 的提前检查由实际提示词和原工具入口承载；执行边界在模型遗漏提前检查时仍执行文件保护。目录存在不等于文件冲突；旧文件存在不等于来源或科学有效性已确认。
- 备份采用独立副本而非硬链接，避免生成脚本原地写入时连带破坏备份。备份不等于原科学任务完成。备份文件和目录已 fsync；审批回执只完成关闭写入，未宣称断电持久性。复制失败可能已登记 attempt/预算，但回执仍明确 command_started=false。仅确切目标的本次审批授权可生效，不从自由文本推断全局覆盖许可。
- 未经当前正式版本/内容核验的诊断文件，即使读取成功，也不升级为已验收产物。恢复可以继续使用原 revalidate/retry_step/ask_user/revise_plan；必需交付的删减仍需用户明确改变任务范围。
- 旧测试中“普通 y 批准覆盖”和“无进展后仍调用一次总结模型”的期待必须按本轮行为更新。故障测试若专测模型超时/畸形总结，保留该测试目的并使用仍允许模型总结的停止原因，不以全部跳过掩盖故障覆盖。

## 验收与交付

最终独立测试：545 通过，20 项已确认的基线失败；新增专项 89/89 与阶段 E 图链 9/9 通过。详见 `test_results.json`、`test_matrix.tsv`、`source_manifest.json` 和 `sync_receipt.json`。所有模型响应均为显式离线替身；小 Python 进程只写有标记的合成表。原始坏候选仅读取并进行静态准入，没有执行。

真实模型/API canary：**NOT_RUN**。真实 provider 缓存复测：**NOT_RUN**。原数据科学 E2E：**NOT_RUN**。Java/snpEff：**NOT_RUN**。未安装、未读取凭据或启动脚本、未 push。

## 回归说明与恢复材料

20 项失败均有进入本轮前的基线依据：13 项旧 Recovery/压缩边界，以及 6 项 reproduce.sh 文本断言和 1 项旧报告路径断言。未通过修改这些断言掩盖失败，不能称全仓全绿。六项复现脚本失败前已通过零总结调用/未Commit断言；后续重入断言未执行，由新增停止专项另行覆盖。

两项压缩测试原先因本轮提示新增 1,346 字符超过测试专用 selector 上限，未进入其预期的 CAS/摘要应用分支。只校准这两个测试为 42k；新增原 40k 负边界仍拒绝请求，生产 policy 未修改。`c3_fixture_capacity_classification.json` 保留基线、反事实和最终证据。

当前已验证对象包含用户先存 overlay。`logical_overlay_delta.patch` 固定本轮增量（相对于 `/tmp/biomind_output_safety_20260916/candidate_baseline`）；Git 提交只包含本轮逻辑，不把 HASH_OUTPUT_POLICY、managed_stop.py 等先存修改据为本轮。原有安全快照在 `/tmp/biomind_output_safety_20260916/original_baseline`；未归档凭据或启动配置。实际目录保留 retriever.py 的原有 CRLF，候选为 LF，归一化文本一致；它未被本轮同步改写。
