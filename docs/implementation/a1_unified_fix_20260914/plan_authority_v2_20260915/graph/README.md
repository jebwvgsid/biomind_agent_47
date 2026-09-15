# V2 完整图验收分册

本分册完成 8 个完整图场景及 4 个新增公开协议验证，共 12 个唯一 nodeid，均取得范围内 PASS。完整图中 5 个真实到达 task_verified，3 个准确停止且未冒称成功。最终索引、各实际版本、返回码和范围见 [verification.json](verification.json)。

最终生产参考为 current14，source signature `5b99904ba6d63cf4486234cddaeb2b339a959b2a562cb40cc6352202ef781212`。语言拒收→正式停止与新增 4 项在 current14 实跑；其余 7 个完整图按实际加载模块、单函数差分及固定 responder 分支复用已有通过结果，见 [最终复用证明](reuse_to_final14.json)。公开请求的示例字节有变化，不能称完整请求相同或真实模型行为已复验。

|场景|实际通过版本|直接验收|
|---|---|---|
|generic_split|current11|8 个异名步骤；独立通用只读检查和通用本地写入后接领域链；两通用步骤均 operation=execute|
|renamed_repeated|current07 fresh retry|7 个异名步骤；同一领域 operation 有两个不同实例；动态 current_step_id 查找|
|generic_merged|current07|7 个步骤；同一通用动作实际解析 ANN 并生成摘要与三个报告，非固定领域 helper 假合并|
|generic_recovery|current11|9 个步骤；首次通用读实际 rc7，真实 Recovery 指令、重新审批、纠正后 Commit|
|generic_blocked|current11|依赖实际 rc23，Recovery 正式 blocked；不生成下游 ANN/摘要，不宣称成功|
|advisory_context|current07|同一通用读步骤带四个公开 Skill 上下文；实际 warning 保留但不占纠正预算、不触发 Recovery|
|generic_no_observation|current07|实际读进程 rc0 却没有声明 observation，禁止 Commit 和任务成功|
|generic_language_mismatch|current14|批准 Bash 的通用步骤收到合法 Python 载体；真实 scope 拒收后正式 blocked，无代码审批、执行或 Commit|

正例统一核对公开步骤、批准快照、每次脚本审批、实际 approved/executed 字节、attempt 身份、实际 helper 证据、workspace attester、Reviewer、CompletionGate、Commit、终态报告。首末 Commit 使用生产 reader 重载后重放原输入，核对 receipt 与状态代次不变。

通用写入实际先向正式输出同目录的新临时路径写入，读取文件大小，然后原子改名；临时路径在审批正文可见，没有独立 Plan output/artifact 声明。实际 stdout 中 helper 证据记录改名前大小与改名后临时文件不存在，最终输出由真实证据链接纳。该例证明这个受审、受控实现可以使用内部临时文件，不证明对任意系统调用的全面沙箱隔离。

候选直接提交 resource_contract_version=3 / resource_uses；没有在 fixture 中补旧 inputs 或 resource-side role_associations，没有重复 TaskSpec 输出 required/path policy 或 task_output_materializations。配置文件与可变工作目录是 Plan 本地资源，保留各自状态和证据义务。

边界：外部模型只有固定业务回复；科学算法与 Java/JAR 能力为新建替身，不验证真实模型可靠性或科学 E2E。继承 TaskRouter 的 intent_admission=test_fixture 及 Consultant 外部 preflight 替身。runtime readiness 是对新建替身文件的显式观察，不是正式主机能力证明。真正 PlanReview 调用生产 _prompt_user，只有 safe_input 的明确 y 为测试输入；代码审批沿真实 interrupt/resume。执行沿现有 guard、非登录 shell、受控最小环境，仅运行本轮审批生成脚本。重载为同一测试进程内生产 reader，不冒称独立进程整图恢复。

同等义务对照见 [same_obligation_comparison.json](same_obligation_comparison.json) 和 [可直接阅读的候选文件](candidates/README.md)。旧 F6 normal 与 renamed_repeated 均保留 3 个输入、5 个 required 用户输出；新目标保留原目标全文并明确组织自由及检查义务。旧 Plan 的实际已准入快照可读，原 provider response 未被旧 driver 保留，明确标记 NOT_RETAINED，只有生产原 raw digest；新 Plan 则保留精确原始 UTF-8 业务回复。不能把两者都称为原始 response。

核心 6 个操作中，原 14 个 input 引用保留为 14 个 read use，新增显式 2 个 mutate use；3 项领域 role 义务保留。移除了资源侧重复的 3 个 step ID 和 3 个 capability ID。此处不主张全部 JSON 字段或字节减少。generic_split 另有明确新增的 preflight_log 任务输出，作为第二例保存，不用它冒充完全相同任务。

[末次公开接口缺陷账本](late_public_protocol_defects.json) 保留两个首次遗漏及修复证据：script blocked 例子与 parser 的 type/action_type 失配，以及 Bash 步骤展示 Python action 例子。current10 的语言用例虽 pytest PASS，却只证明拒收保护，正式停止义务 FAIL；最终改为直接消费实际请求例子并断言原生 GENERATE_STOP_REQUESTED。current12 的 Bash 输入路径遗漏与 Python 测试读取字段错误均保留，current13 仅导出未运行，current14 五项全部通过。公开 Bash/Python action 例子只是会主动失败的表示模板；本次新准入测试未运行它们，不把携带路径称为实际读取或科学结果。

早期真实故障、180 秒外部测试看门狗退出、宿主中断 UNKNOWN 和旧断言失配均在 development_* 及各版 runs 中保留。witness、pytest 和正式科学任务成功分开记录；本分册不声称真实模型或真实科学 E2E 通过。
