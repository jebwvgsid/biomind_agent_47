# BioMind / Pi 实施交付（2026-10-02）

本目录对应《修改实施 Spec v1.0》，原审计 `docs/audits/pi_unified_environment_20261001` 保持冻结。实施基于当前有效工作树，不能把干净 HEAD 当作本次运行基线。各项结果以 `acceptance.tsv`、测试日志和构建清单为准；合成执行、生产元数据读取与自然模型行为分别记录。

**交付状态：代码、实际 dist 与本地分批提交已完成；宿主稳定性仍有一项未定因问题。** Python 88/88 通过。Node 第二轮完整批次为 166/173，随后 17/17 相关用例复跑通过；host 普通流程与 memory-auto 的独立复跑分别通过。完整批次及有限复跑中观察到的 SIGKILL 仍不能解释，因此没有声称“整套单轮全绿”。48 项验收为 47 PASS、A02 PARTIAL；具体限制见 `C.md` 和 `host-signal-observation.json`。

## 实际修改

**A：登记与安装可靠性。** PM 的既有写入口统一使用稳定锁、锁内重读与合并、同目录原子替换。缺省字段不清空，身份冲突和坏库不静默覆盖；保留 prefix/runner/版本/扩展字段及不同强度的证据。安装仍使用原 propose/execute/claim：目标建立之后，用目标入口/runner/库变量执行结构化检查，再一次提交完整登记。安装后登记失败只恢复登记；取消和丢失回执保留实际副作用，不声称 exactly-once 或软件回滚。

**B：环境复用。** 查询、补登记和安装共用已解析 RegistryView.path；能力查询允许缺 Skill 映射的已登记候选。选择以请求和运行时 scope 保存到既有 runtime_evidence，run_command 消费唯一适用绑定，多绑定需明确 ref。原名 bash 增可选 binding_ref，逐次复用同一 Python 启动解释；裸 bash 和用户 `!` 保持宿主语义。补登记与安装分开，模型声明不会升格成宿主验证。protected 中 prefix 元数据不等于目录授权。

未命中才可调用 select_environment 的 locate 模式：原 executor 运行固定只读元数据程序，最多 3 个候选、单段 5 秒、总 15 秒、8 KiB 摘要；无递归搜索、module、登录 shell、版本/import 或候选工具执行。同一请求的同范围定位复用已返回结果。定点执行诊断使用 probe_command.environment_diagnostic，默认单次 15 秒、每请求 30 秒的保守预留预算，call 幂等；快速结束不自动返还预算。用户明确检查/QC、正式计算及安装不使用这项短预算。

**C：统一主 agent。** 主工具集合按 native/protected 的实际边界提供能力；新请求不再创建/消费 handoff，不再选择 Consult/Generate 提示或交接续轮。Domain 使用宿主 work 请求准入，不伪造 Generate。真正的新请求隔离、用户修订/停止条件、来源撤回、Pi 压缩和原安装/作业 owner 保留。旧角色数据只读解码；被撤回的历史材料不会重新注入，当前宿主绑定的用户原话继续有效。

enrichment Skill 去除例行版本/import 检查，保留 base-R ORA / GSEA 依赖边界、显式 R/fgsea 失败停止及科学输入 QC。正式计算仍用原 observer 的 run_ref、持久日志和真实取消/超时状态；没有新调度器，也没有把任意 bash 自动变成托管作业。科学包装保留上游退出码，产物审查复用既有 validator/provenance，允许契约规定的合法空结果。

## 生产 rna_env

实际权威为 `/public/home/liusiqi/03.毕设/agent_shim/software_registry.json`。通过新 host 补登记入口记录 `/public/home/liusiqi/anaconda3/envs/rna_env` 与准确 Rscript 入口；证据来自指定历史会话，时间 **2026-10-01T08:26:16.342Z**，R 4.5.3，fgsea 版本未知。其余登记精确不变，未修改 Conda 环境或永久绑定所有 enrichment 到本机。

`rna-registration.json` 保存相关行前后像与来源；`rna-fresh-reader.json` 是新 Python 进程/临时新会话零子进程查询。它们不表示今日重新检查健康或真实分析已启动。TAD、ORA、R/fgsea 和 reproduce.sh 均未重跑。

`supplement_rna.py` 默认只预览，`--apply` 已执行一次，禁止为了验证重复补登记。需撤回时使用该脚本的 `--rollback <rna-registration.json>`：只在相关当前行仍等于本次后像时回退本次字段；并发变更会明确拒绝，不能整库覆盖。生产回滚未执行。

## 有效工作树、Git 与恢复

实施前 HEAD/审计提交：`4721cc8e737991b7d6571d5bebfdf47e808b6ba1`；已有 107 项 tracked 修改/删除、5005 个 untracked，index 为空。专用分支 `refactor/pi-unified-runtime`。没有 reset、stash、clean、amend、rebase 或 push。

本轮每批用编辑前像生成精确 delta。可独立应用于原 index 的块进入源码；依赖既有 dirty 内容、不能独立应用的块以 `deltas/*.patch` 提交，实际工作文件保留修改。每个文件的处置见 `deltas/A-staging.json`、`B-staging.json`、`C-staging.json`。这是明确的未提交依赖，**仅 checkout 新 SHA 不能完整复现有效产品树**，不能把旧改动混入提交来掩盖这一点。

恢复包位置、SHA-256 和范围见 `baseline-recovery.json`。恢复应在独立临时目录从审计 SHA 开始，应用包内 `preexisting.patch`，恢复 `untracked/` 的相关原代码，核对 `before/` 与基线摘要，然后按 A→B→C 应用本目录全部 delta 一次。不要既 cherry-pick 源码块又再次应用相同 patch；不要在当前用户工作树覆盖恢复。原依赖安装和 Pi 补丁不包含在包中，复现时仍需同版本已有依赖。

`verify_delta_chain.py` 已在临时目录重建：64 个 A/B/C 补丁还原的 51 个变更文件与当前有效树逐文件 SHA-256 一致，见 `delta-reconstruction.json`。该结果证明变更链可恢复，不等于未携带基线的干净 checkout 可运行。

生产 registry、历史 session、科学结果、软件环境和凭证未纳入代码提交。与本轮无关的数千个档案未打包。提交 SHA 见最终交付清单。

## 验证与限制

验证命令和范围见 `REPRODUCE.md`；原 32 项和新增 16 项逐项对账见 `acceptance.tsv`。离线 scripted provider 证明接线和上下文，不证明自然模型必然先查询或遵守所有科学条件。无付费模型评测，无联网安装，无真实跨节点/慢 NFS 故障实验；这些范围保持 NOT_RUN。

已选 prefix 的任意 shell 仅保证 PATH 优先，不能保证 shell 文本内每个程序都属于该 prefix；精确 argv/结构化安装验证则检查真实入口。entry-only 登记没有兼容 shell runner 时明确 unsupported。原 observer 同一任务只允许一个活动托管运行；并行作用域隔离由独立共享执行与带绑定 bash 夹具证明，没有增加第二套调度。

构建使用当前已安装 TypeScript、Node 22.22.0 和 Pi `0.87.1+biomind-observer.3`。实际启动链仍为 `~/.local/bin/biomind → bin/biomind → bio_agents.py → agent_core/pi_entry.py → integrations/pi_runtime/dist/host.js`。dist 按仓库原规则忽略；源与产物散列、补丁校验和完整构建结果见 `build-C.json`。未终止或重启生产作业；后续新启动加载新 dist，已启动进程继续运行其已加载版本。

序列化对比使用同一个已安装 serializer、模型配置、输入和两次请求夹具，见 serializer-before/after/comparison.json。单位为字符和 UTF-8 字节，不是 token 或账单；工具变多的成本完整纳入。该夹具未触发压缩或重试，不能外推自然工作负载的节省比例。
