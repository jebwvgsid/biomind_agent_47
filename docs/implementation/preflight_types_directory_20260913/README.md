# 预检类型、证据目录与 Recovery 公开投影实施回执

起点 `6cd529f5a6e91f2a87bd4ac77ec321cad19445ee`，分支 `refactor/consultant-controller-authority`。在当前源码上增量实施，未恢复 session_1789266028，未读取其原输入或数据库。本次使用已提交审计结论，不重复现场审计。原437条 dirty/untracked 状态和五个历史 dirty 文件保留，不收编它们。

## 实施结果

**PF-TYPE/RESULT**：FASTQ batch 保持 FASTQ-only 和 nargs=2，原负观察语义未删除。新增 `inspect_fasta_header.py`：plain UTF-8 FASTA 头部及一个序列字节，默认头部4096字节、原始输入8192字节、协作时限2秒；外层仍有原短检查超时和取消机制。17000碱基序列行无需扫描到换行，测试实际读取6字节。gzip参考明确不支持，头部超限/读取预算/权限/缺失/超时如实返回错误；不宣称全部contig或全文件完整性。FASTQ原限额没有提高。

新增薄入口 `inspect_wgs_inputs.py`，显式 `--fastq LABEL PATH`（重复）和 `--reference LABEL PATH`，委托 FASTQ 与 FASTA 两个reader。现有 `run_checks` 已支持串行编排、真实进度、超时取消和精确日志，因此没有改造编排/执行器，只组合这两类已确定需求。一个外层 `validation_observation` 汇总完整 facts：每项scope、finding、raw refs、returncode，以及未执行项；没有两个同名marker覆盖。`all_checks_passed` 来自全部真实finding，技术状态、负观察和退出码分开。任一adverse/incomplete时仍保存部分证据，组合入口返回2，原失败处理链继续负责判断。

Skill更新为1.1.1，正式execution_guidance、说明和help同步类型/目录语义。新候选声明组合入口、FASTQ reader、FASTA reader和batch资源四个handle，经现有registry冻结文档与脚本。测试脚本使用binder给出的环境句柄，没有把导出根绝对路径当作Skill授权。已有生成/批准正文不被改写；过期资源经原漂移校验拒绝。

**PF-DIR**：采用规格方案A，在既有 `execution_policy_manifest` 增加局部 `evidence_binding`，纳入原approval hash与四来源审批正文。根来自已有 `data_path/task_outputs/<task>/attempts`，用途是内部执行证据；不是从用户目标文字、cwd或历史路径猜出“输出根”。用户结果目录与此内部日志范围分别保留，本次没有扩大Plan outputs。

Generate最终消息给出受控 `BIOMIND_PREFLIGHT_EVIDENCE_DIR` 引用和批准父根。执行边界完成原approval/资源检查并取得正式attempt后，解析为 `<approved_root>/<actual_attempt>/preflight`。解析不创建leaf；helper是唯一创建者，继续 `mkdir(exist_ok=False)`。路径检查复用approved_resource_locator，拒绝链接/冲突/错误根；声明环境不能覆盖保留句柄。具体绑定进入本attempt侧录及正式ExecutionRecord，关联action、attempt与policy hash。helper在受控调用中核对传入目录与该绑定完全一致，类似前缀的兄弟目录不被接受。它不是任意shell的权限沙箱；原代码审批、原路径/资源检查继续适用。

新attempt换新leaf；已返回attempt的生产Execute重复投递走原幂等恢复边界，新增审批0、新增进程0，原执行账本逐字节不变。没有删除旧目录、改exist_ok、覆盖旧证据或批准后替换脚本文字。

**REC-DISPLAY**：native拒收分支公开explanation为空，系统状态显示“恢复提案尚未接纳”及真实错误code/协议字段（例如schema_version）。不从content按长度、语言或前缀选文，不把原长content移入details。格式校验通过的native候选显示当前directive.summary，并说明尚待接纳；真正接纳仍由原admission事件显示。系统状态与模型摘要使用不同item_id，避免去重丢摘要。合法只读工具回合仍有公开说明/真实活动；空回合不复用旧文，取消不产生接纳。

原assistant、ToolMessage、explanations、接收/纠正历史与预算保留。真实Recovery→Generate重试验证确认 `unverified_diagnosis_not_execution` 里的原合成诊断仍存在，参数、类型目标和运行时目录引用进入实际最终provider messages，格式纠正保留消息前缀。没有新增旁白、翻译或补状态模型，也没有context-loss修补。

## 修改入口

| 切片 | 文件与函数 |
|---|---|
| REC-DISPLAY | `agent_core/workflow/recovery_agent.py` 响应收口；`bio_agents.py:_schema2_recovery_event_sink`；新增公开投影测试与白名单导出驱动 |
| PF-TYPE/RESULT | `agent_core/inspection_readers.py:inspect_fasta_header`；mapping Skill 文档；batch `main` help；新增FASTA `main` 和混合入口 `main/assess_input`；新增小输入测试 |
| PF-DIR | `agent_core/action_runtime.py:build_execution_policy_manifest/resolve_preflight_evidence_binding`；`bio_agents.py` Generate消息、`_bind_pending_action`、`execute_logic_impl`；`execution/preflight.py:run_checks`；`agent_core/recovery.py:ExecutionRecord`；精确子进程环境测试白名单、目录与mapping提交测试 |

Plan、CompletionGate、typed routing、业务预算和审批按键均未修改。没有新增Controller、TaskSpec、通用语义binder或格式插件体系。

## 验收范围与证据

| 规格 | 实际断言 |
|---|---|
| T1/T2 | 中文/空格路径真实reader；LABEL/PATH分开；旧合并形式仍argparse错误，不自动拆分 |
| T3/T5 | 短/17000碱基单行FASTA通过有限检查；gzip不支持；空/错误头、头过限、输入预算、超时、缺失和不可读有明确负/未完成结果 |
| T4/T6/T7 | 完整五项与两种scope；短FASTA放FASTQ时子rc0但finding=false，组合不成功；长FASTA误用保留READ_LINE_LIMIT；一项技术失败保留已成功项和unstarted项；生产失败链不提交预检 |
| D1/D2/D3/D5 | 批准policy真实根、本attempt绑定与正式记录一致；执行前leaf不存在；预创建、旧目录、类似前缀兄弟目录和旧环境绑定拒绝；第二attempt不同leaf，旧原始日志hash不变 |
| D4 | 两种显示模式各通过生产Execute重复投递2次：均新审批0/新进程0/新账本记录0 |
| D6 | 实际mapping资源注册；wrapper、FASTA reader或Skill文档变化均拒绝旧绑定；新候选取得新快照 |
| U1/U2/U3 | native长content不进主区/详情；错误字段与当前短摘要可见；原历史进入下一provider请求；4轮/2工具/1纠正预算不变；空回合不复用；取消0接纳 |
| G1 | 真实mapping参数失败→Recovery缺字段拒收→补交接纳→Generate格式错误→纠正→第二真实attempt→正式CommitStep1次；无Planner重入 |
| G2 | 真实mapping注册/四脚本绑定→原Plan批准→Generate最终消息→原代码审批→真实五reader→完整observation→原Supervisor→CommitStep1次；没有local-blast替代绑定、绝对wrapper旁路或总是通过的gate |
| G3 | compact/off固定响应：调用、工具语义、路由、批准代码与原始观察一致；不同合成根与已记录的本attempt leaf明确替换比较，逐案保留原件SHA，不声称不同attempt原hash相同 |

正式纵向正例只含当前预检步骤，outputs=[]、validation_observation来自本次真实执行。Plan编译、批准、解析、Supervisor和CommitStep均为生产路径。fixture仅提供新任务/模型响应、必要的无科学环境探测替身与人工批准答复；没有替换gate/verifier/commit。此inspection capability不声明BWA/GATK运行时依赖，测试没有检查或调用这些程序。语义reviewer本例不需要新增模型，使用原配置下的确定性核验。

负例将短FASTA作为声明FASTQ交给实际reader：它返回0但prefix_valid=false；真实汇总保留4/5 passed，外层失败，原execute_outcome=execution_failed，CommitStep为0。这个反例同时检查原始finding、汇总和正式失败链，不以改非零码单独充当验收。

## 实际命令与测试来源

每片先暂存本轮独占文件/hunk，生成待提交Git tree，再使用 `--committed` 只叠加五个原有安全dirty文件；生产/测试字节来自该tree。通过后才commit。`verification.json` 保存每次实际tree、source signature、测试argv、rc、pytest phase、失败账本和产物hash。

```bash
cd '/public/home/liusiqi/03.毕设/Biomind_v1'
# 本轮实际使用的三个待提交tree已记录在 /tmp/.../{rec,type,dir}-tree.txt
python tests/export_preflight_implementation.py /tmp/biomind-preflight-implementation-20260913/dir-tree-export-2 --tree "$(cat /tmp/biomind-preflight-implementation-20260913/dir-tree.txt)" --committed
python - <<'PY'
from pathlib import Path
from tests.cli_model_progress_support import launch
raise SystemExit(launch(
    Path('/tmp/biomind-preflight-implementation-20260913/dir-tree-export-2'),
    Path('/tmp/biomind-preflight-implementation-20260913/dir-tree-check-2'),
    ['-q', 'tests/test_preflight_types.py', 'tests/test_preflight_directory.py',
     'tests/test_recovery_public_rejection.py', 'tests/test_preflight_mapping_commit.py'],
    fatal_diagnostics=True, installed_cli=True))
PY
```

示例路径为本轮实际记录；重跑应选新的export/results目录。子进程解释器 `/public/home/liusiqi/anaconda3/envs/biomind/bin/python`，复用已安装CLI依赖，未安装/升级。保留原fd capture、双guard与独立持久fatal出口。测试只向精确注册的新脚本放行精确的合成环境，没有关闭网络/进程guard。

通过结果：REC候选树4项（含既有工具进度/显示故障回归）；TYPE候选树3项（含原8组真实batch审计witness）；DIR最终候选树的14项为本轮完整目标验收。上述批次有重叠，不相加当作独立用例数。 最终14项这一批共25次明确计数的fake provider调用（图C4/P4/G6/R2，Recovery组件9）、4次组件只读工具stub、14次真实batch wrapper、40次嵌套reader和5次独立reader。图内4次Plan批准、5次代码批准/执行attempt、3次预检正式提交；其中正常两个显示模式各提交1次，失败后修正提交1次，adverse负例提交0次。另有4次Execute重复投递，新增审批/执行/账本记录均0。真实模型/API/BWA/GATK与新增旁白调用均0。旧审计脚本和断言原样保留；其“旧拒收全文公开”见证不作为修复后应维持的行为。

前期失败全部留档：fake Plan缺必填字段/使用内部artifact ID而非公开handle；原content收口strip导致测试断言差异；系统状态与摘要item_id冲突（已修生产接线）；现有guard起初未放行精确绑定环境；新参数反例的argparse文字与历史不同；幂等fixture误要求全局pending_action而非实际typed Generate输出；最后补充的状态断言误写analysis的done，现按原validation-purpose状态精确断言passed，同时要求真实CommitStep回执。未跳过断言、未清理失败记录、未修改业务规则来迁就fixture。

## 限制

本结论仅适用于列明的新合成输入和真实接口/绑定范围。没有恢复原WGS，没有证明真实模型能稳定生成同样候选或后续WGS结果正确；没有宣称全参考/FASTQ完整性通过。历史loaded revision、整体清理状态及原SIGSEGV仍属此前未解决范围，新PASS不代表原生崩溃已修复。

所有代码、测试和文档分片提交，不push。每片完整hash、parent和文件列表见 `verification.json`；最终回执另给文档提交hash，本报告不内嵌自身hash。
