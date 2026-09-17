# BioMind 环境、安装恢复与 binding 统一修复回执

生产修改与离线联合验收已完成；服务器上的真实 bubblewrap 受控发现仍被权限错误/超时阻塞，不能将这部分标记为实际主机验收通过。未安装或升级软件、未调用真实模型/API、未恢复旧科学 session、未运行完整 WGS。

执行依据是本轮统一任务书及两份已存在审计。统一任务书引用的另外两份详细任务书未在当前附件/仓库找到，未据名称补造额外要求。

## 实现对应

| 范围 | 实现与结果 | 主要位置/证据 |
| --- | --- | --- |
| R1 | 按实际 rollback affected producers 和准确 Plan 失效旧 accepted 索引；保存重载及新 Commit 后旧事件不能撤销新值，保留不可变 binding 规则 | `plan_tracker.py`、`plan_bindings.py`；`notes/r1.md` |
| R2 / A1 / A2 | 已选 Skill/capability 独立状态；统一固定 argv 探测、证据、resolver、批准与目标检查；显式过期事实不能从 JAVA_HOME 派生绕过；coverage 缺口保持 UNKNOWN | `runtime_probe_contract.py`、`runtime_inspection.py`、`action_runtime.py`、`node_context.py`、`bio_agents.py`；`notes/runtime_integration.md` |
| A3 | 旧 action/空 manifest 不回填；验证结束证据、正式修订或当前恢复授权，终结 ownership 后建立新 epoch；同 prefix 准备后可一次性重新发现，接受新事实才取代旧否定证据，继承已退役历史；同一步生成、批准、真实小脚本执行及 Commit 已通过 | `workflow/runtime_evidence_recovery.py`；`notes/runtime_authority_review.md`、`notes/runtime_graph.md`、`notes/runtime_install_resume.md` |
| B1—B4 | required 缺证据的真实停点进入 AskUser；显式选择已有环境或安装准备，安装具体代码/目标/来源/影响仍单独批准；取消/拒绝终止；显式目标验证与两项登记分阶段、登记失败不重装；人工完成需复验；复用 A3，不代科学 Commit | `installer.py`、`bio_agents.py`、typed outcomes/graph；`notes/install.md`、`notes/dependency_preparation.md` |
| R3 / D1 | root helper 沿所选 snapshot 的显式 delegates 展开去重；错误保留真实 validator/carrier/actual/expected；白名单目录投影；receipt/digest/stat 与 venv 入口保留 | `skill_resource_binding.py`、`skill_script_invocation.py`；`notes/helper.md` |
| R4 / D3 | 最终同一 WGS revision 包含 helper/runtime/产物合同/阶段 reference；真实 reference 读取可达；完整源一致的参考 bundle 经真实 Supervisor/Commit 验收，坏 bundle 拒绝 | 最终两份 WGS Skill、`genomics_reference_bundle.py`、`semantic_completion.py`；`notes/d3.md`、`notes/skill_requests.md` |
| C1 / C2 | 准确 terminal attempt/step ownership；历史已解决检查失败保留但不冒充当前失败；内部诊断报告绝对路径可见、旧文件保护保留 | `workflow/managed_stop.py`、`cli_lifecycle.py`；`notes/terminal.md` |

没有增加平行环境 Recovery/Planner，没有删除 `_not_dispatched`、required 非空保护或 binding immutable 规则。`build_skill_script_argv` 接入真实 direct-argv 校验，未宣称实现仅参数输入的自动动作组装。

## 最终验证

**同一最终候选累计 377 passed、26 subtests passed、0 failed（120.49 秒）**。最终计数和受测身份见 `evidence/cumulative_receipt.json`；逐例、命令、日志分别为 `cumulative_pytest_results.json`、`cumulative_invocation.json`、`cumulative_test.log`。最终测试在一个新进程中加载同一 905 文件候选，验证期间源码不变，结束后与当前工作树逐文件校验。

五组交叉风险均有真实核心链覆盖：rollback→持久化→重跑 Commit→消费；旧空 manifest→正式 revision→新作用域→受控发现进程叶子→新批准→实际 tiny runner→Commit；UNKNOWN/UNAVAILABLE→AskUser→同 prefix 已有环境/显式安装→目标验证与双登记→新 epoch/事实 supersedes→科学动作批准→实际 tiny runner→Commit；helper/runtime 完整批准与变更失效；最终 Skill 请求及按需 reference 读取。模型、用户答复、安装进程和受控检查/目标 healthcheck 进程是明确替身边界；集成夹具关闭可选 Consultant seed/preflight 与记忆旁路。状态、scope、解析、批准、Installer 内图、登记、绑定接纳和 Commit 核心没有预设成功。

额外兼容套件为 **63 PASS / 15 FAIL**，其中审批范围 7 项全通过并已纳入最终累计套件。15 个失败节点均在共同基线已复现的失败范围内：旧 SnpEff/历史 recovery 夹具 2 个、旧 semantic/reviewer 夹具 3 个、旧 Planner 协议 runtime 夹具 10 个。没有删断言、批量 skip 或改生产准入求绿；不宣称整个仓库测试全绿。详见 `evidence/compatibility_final_receipt.json`。原审批专项 19 PASS / 1 FAIL / 1 ERROR 的两项过时能力夹具已按现行 SnpEff required/optional 合同迁移，相关 21 项通过。

局部测试只作为定位证据，不与累计结果相加。旧 helper 的 69 passed 没有被复用为本次结论。独立前置复核为 58 passed、最终安装专项 79 passed；R1 新例 5、D3 新例 20、现行 runtime graph 2、依赖准备完整图 5、同 prefix 恢复新增 13；安装边界专项及完整切片证据在本目录，最终以累计候选为准。

## 真实工具与实际主机限制

已安装软件、独立 384 bp / 2 contig / 4 对 reads：FAI、BWA index/MEM、samtools sort、Picard dictionary/MarkDuplicates/index、flagstat、GATK HaplotypeCaller 真实成功。8 reads 映射、2 duplicates；完全匹配参考的小样产生 0 variant record，不能推断检测灵敏度或完整 WGS 成功。原 GATK launcher 失败保留；成功使用明确系统 Python、目标 Java/launcher 和受限 JVM 参数。命令与产物身份见 `evidence/d3_micro_receipt.json`。

最终固定 argv 直接探测：mapping 7 项 AVAILABLE，`samtools coverage` 为 UNKNOWN/命令不匹配；GATK 5 项 AVAILABLE。未静默升级或替换 coverage 含义。真实 bubblewrap probe 返回非零或超时；独立最小命令报告 NETLINK_ROUTE `Operation not permitted`，外层提权运行同一只读验证仍未解决。代码保留 fail-closed；该服务器的动态受控发现需要先排除此主机限制，不能用离线 process-leaf 通过替代。证据为 `real_runtime_probes.json` 与 `real_controlled_adapter.json`。

真实安装、真实模型/API、完整 WGS：**均未运行**。

依赖准备当前支持已配置绝对 Conda 根目录下的命名环境；`base`、任意外部 prefix 和无法确认根目录的目标明确拒绝。没有把未支持的目标当作验证成功，也没有扩展环境管理器协议。

## 请求计量与最终 Skill

同一最终 mapping snapshot 的真实 Generate messages canonical JSON 为 52,985 字节（message content 50,305）；helper catalog 5,191 字节，同 snapshot 按原逻辑重建的 raw catalog 为 7,199，减少 2,008 字节（27.89%，仅该投影）。不是历史整请求 A/B，不推断 tokens、速度或整机收益；没有引用旧草案 54.65%。原始重复 invocation/stat 与 dynamic header 重复均未进入该实际请求，三个 mapping references 经真实 SkillAccessService 同 snapshot 读取。数值见 `evidence/final_unified-final-skill-request-metrics.json`；隔离路径长度随验证目录变化，未与早期切片的整请求字节作性能比较。

- mapping v1.2.0 SHA-256：`6ef039a31b722cb876bfaa0fc8c5e237417bc77646ab237cfe0d91fffdf4b4fa`
- GATK v1.2.0 SHA-256：`ab73a4316ca944e889f4c6094a9624f3541dfe1cbf870cd50d571d4b86d2f59e`

旧 session 后续必须新进程加载代码/Skill，读取失败证据，确认进程结束，必要时正式修订 capability/Plan 并重新批准。已有 input_check 仅在输入、范围、来源和 Commit 仍有效且不属于实际 rollback 集合时保留。本轮未恢复 session，也不声称旧进程热更新。

## 提交、前置与复现

起始 HEAD 为 `73e1f8fbc585c695530fba01d4e5394b99b4cbcc`，分支 `refactor/consultant-controller-authority`，初始 staged 为空。共同前置包含当时未提交代码；审计 HEAD 单独不等于受测基线。`evidence/baseline.json`、`initial_status.txt` 与 `baseline_dependency_addendum.json` 固定前置身份。

已单独复核保存三个 helper 前置文件，提交 `078f7198e8c5c4447affefa6bfdbdf14e7de5152`；第一组生产增量为 `2e70f28658b8aee0d7cd9451f7f353f4b4121825`。完整依赖准备与恢复入口补充提交为 `d2a35c4834c15373841c5e2969281c11c4db6da7`。完整受测身份见 `evidence/implementation_commit.json`。最终维护文档提交以本文件的 `git log -1 --format=%H -- <path>` 为准，最终回复也提供完整身份。

**受测对象是实际候选，不是脱离保留前置的单独 Git HEAD。** `evidence/commit_partition.json` 与 `followup_commit_partition.json` 列出逐文件暂存身份和保留前置差异；已有 dirty 未整包提交。重叠文件以共同前置、相应已测试检查点为 base，仅合入本任务差异；首个增量中 `bio_agents.py` 的唯一目录投影冲突采用本次完整替换。没有 reset/clean/stash/push，未触碰凭据。

完整前置源码、其原始 tracked patch 与 47 文件的精确 `task_increment.patch` 保存在 `/public/home/liusiqi/03.毕设/biomind_audit_exports/environment_binding_unified_20260917/`。最终 905 文件 manifest、增量 manifest 和 reconstruction identity 都已保存；重建使用明确文件，不导出 config/startup、隐藏文件或凭据。

```bash
python docs/implementation/environment_binding_unified_20260917/tools/reconstruct_candidate.py /tmp/biomind-rebuild-NEW
python docs/implementation/environment_binding_unified_20260917/tools/run_validation.py /tmp/biomind-rebuild-NEW /tmp/biomind-validation-NEW
```

第二条使用已安装 biomind Python、空环境、合成配置和既有 offline bootstrap。第一次在新目录中校验前置+补丁重建全部 905 个文件，再启动新测试进程。不可覆盖原工作区或旧科学 session。

暂缓项仍为统一任务书列出的 D4、C3、E2、E1 性能计量、全链 optional 扩展、身份/receipt/digest 删除、通用 shell wrapper/loader 重写等；未借本轮修复扩展这些范围。
