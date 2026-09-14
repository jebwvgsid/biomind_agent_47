# Generate / helper / 当前事故审计

本分册结论来自指定 session 的原始公开请求/业务响应/脚本/回执、当前源码、及新隔离进程。原 session 从未交业务 loader，原脚本未执行，正式科学文件未读取。`evidence.json` 给原路径、SHA-256、JSON pointer与最小片段；`interfaces.tsv` 13项接口、`guards.tsv` 15项拦截均标明证据等级。88个活动 Skill / 96个 normalized capability 的注册分母见总报告 registry_inventory；这不意味着96条科学执行均验证。

## 首因与紧接着的共享故障

**IG-D1：框架承诺的 marker 没有在合法 argv-only 动作中物化。**

原 `generate-0009…/response.json /content` 是 `action_type=execute, operation=check_contig_compatibility, argv=[bash,-lc,<script>], env={}, outputs=[]`，没有 `code`。`argv[2]` 正确含 `# BIOMIND_EFFECT_HELPER`。当前 parser（bio_agents.py:21729、21810）保留合法 argv 但得到空 code；Generate 的 native helper 物化（43889—43908）只处理 code。`materialize_effect_template('')` 返回空串，argv 脚本从未展开。原 `approved.sh:4` 是 marker；92行为未定义 helper 调用；`executed.sh:6` 直接执行批准脚本，不提供 Python namespace。原 rc=1 / stderr:3 为 NameError。这不是模型省略 marker、脚本提取掉定义、或 Python 子进程偶然丢作用域。

当前新 snpEff 原生 graph 已见证 argv-only 可以走到 `action_ready` 且仍无定义；相同 code 入口会展开。小进程在nonlogin shell执行上述**新合成候选的相同body**，分别 NameError 与成功发射 False（完整native argv的login启动行为未作为安全通过项）。小进程是 executor 组件见证，不能冒充完整动作 Execute/Commit 全链。

**IG-D2：普通、Recovery、局部纠正共用 parser，但公开格式信息不等价。**

0009 `/messages/1/content` 要“一份 raw JSON、无 Markdown/prose”；紧接着 `/messages/2/content` 的公开 helper rule 要“ONE complete executable block + short explanation”。0011 Recovery 切换 `start_generate_after_recovery` 后，正式 seed 替换旧 prompt：仍有 capability 字符串“one action_json execute object”，但没有普通 native prompt 的 raw JSON 完整样例，helper相冲突 rule 保留。0012 局部纠正继承这组消息并附上完整此前业务候选、结构化错误与预算；不是证明整个上下文丢失。

0011 返回 bash fence+公开说明；说明声称替换 helper，实际脚本仍旧调用。parser 尝试 fence/raw/greedy brace，最终选择 Python `{norm(n) for n in fa_set}…` 片段，在 JSON 第1行第2列报 `JSON_DECODE_ERROR`。这是**动作协议**解析，不是 stdout执行证据解析。

0012返回有效JSON `{"action_json":{"operation":…,"code":…}}`，parser不把该任意外壳当正式action：顶层缺 action_type，报 `ACTION_SCHEMA_REQUIRED_FIELD`。这两次拒收本身应保留。最小设计应将当前协议和完整示例随既有 seed 传入、helper rule依当前协议生成，而非再造新的控制层或宽松猜字段。

**IG-D3：即使helper已展开，原 facts 形状仍将被拒绝。**

原调用为 `emit_effect_evidence({'contigs_compatible': {'value': compatible}})`；公开声明提供按category组织的 declarations，但规则只说 identity-keyed actual_facts，没有一份本步完整helper调用例。真实 serializer 要 `{'produces_observations': {'contigs_compatible': {'value': actual}}}`。新隔离Python/Bash实跑前者得到 `EFFECT_FIELD_UNDECLARED`，后者 False 被真实证据 parser保留。不能用假 True掩盖次生失配。framework template只支持artifact/side effect/observation三category，workspace_mutations和candidate bindings有独立接口；不能宣称helper覆盖所有效果。

**IG-D4：identical标签错误地把绑定事实无增量当作全部候选无进展。**

原两拒收事件：

| 项 | 0011 | 0012 |
|---|---|---|
| candidate fingerprint | `cdfa1353c233234e7c2c69dd2752399ffadfb6dee8b6c903d59acbca7ec047f7` | `ba0c5ca618726578ec479da5e9865fb7bcfca11d7d336e8ebd18c5d1fb2a344c` |
| error fingerprint | `fce0ddda5549177bb52471f9f4492b3273db3a28a934966fd9862128a7fc53cd` | `f9737de13430ec2ab784095add715c36a14381ff1113563022defd78e1bff00d` |
| public_valid_facts | `[binding_env]` | `[binding_env]` |
| status / progress | repairable / true | internal_error / false |
| corrections authorized / remaining | 1 / 3 | 1 / 3 |

不存在空指纹回落；原脚本也并非逐字相同（set comprehensions改写、norm_ok删除），但这些代码改动本身并不证明科学修正。真正的表示变化是未解码→可解码JSON，仍需修正外壳/类型。bio_agents.py:16525先比较候选/错误指纹，16533在任何 frozen binding input 下用`binding_input_progress_facts`增量覆盖结论；该函数只识别operation/bindings/binding_env，忽略JSON解码、action_type、code/argv和env字段错误。新 graph进一步证明：第一轮已同时缺action_type且env某值非字符串→第二轮只补齐action_type、保留同一env错误（严格减少一个表示缺陷），两个不同指纹却仍被误停remaining3。保留绑定安全核验和持久预算，调整原节点的表示层进展/重复判断；不能仅放大预算或以错误码变化续命。

**IG-D5：另一个当前解析后故障——非法env父类型导致指纹异常越过局部纠正。**

新候选`env=['invalid-parent']`先被真实parser识别为格式错误；在拒收receipt前，bio_agents.py:43987却对原候选调用仅适用于合法动作的managed_action_fingerprint，managed_artifacts.py:513的`dict(env)`抛ValueError。当前路由给`generate_exception`且没有第二个拒收ledger事件。该非法env必须继续拒收；修复应保留原parser精确错误，让原候选总定义指纹函数承接，而非接受非法env。

## 版本证据与既有工作

原0009/0011/0012 manifest都引用runtime identity对象`53a0…3673`；其显式源码snapshot内bio_agents、step_effects、plan_bindings、prompts/manager字节与本轮当前工作树一致（hash见evidence）。它还记录step_effects、plan_bindings等模块marshal_v2 callable hashes。`bio_agents`未在loaded_modules中（以`__main__`启动是合理可能，但不能据此补造加载证明）。原durable workflow_routing_schema_version=2；selected_skill_refs有冻结snpeff snapshot_id/content_hash，当前Skill字节与该hash一致，但manifest.skill_versions为空。原`loaded_revision`仍为UNKNOWN；response archive只有content/id/role，没有finish_reason。manifest completed不是无截断的完整provider证明。当前隔离进程的export/source/load回执另列，不把disk HEAD当历史已加载版本。

现有S1 partial publication、helper marker/冻结、S2额外调用总额、完整 malformed digest、script blocked、source identity、S3报告journal均已存在。原事故三个report_publications均status=published，有sha和ownership receipt；因果packet已消费并保留原attempt。此处没有复发“报告拒写”或“原因果丢失”的证据，不应重做这些修复。

## 最小联合实施接缝与验收

1. 先在现有Generate协议选择/seed投影和`declared_effect_emission_contract`处保留一份准确的当前wire说明、完整action与本步facts例。普通、Recovery、局部纠正必须复用；支持的主动blocked写法也随之公开。保留existing stopped outcome权限。
2. native argv/code适配与marker物化合并为一个审批前流程：只对已有正式支持的argv语义作确定性提取/回填，拒绝code与argv不一致；冻结后的body、argv、template hash连同审批对象一致。不要审批后补helper、靠扫描脚本语义或任意解包shell命令。
3. 同时修复格式候选指纹的总定义性与原节点进展判断。区分representation成功、绑定字段修正和执行脚本修正；保留完整candidate/目标错误的历史，准确拒绝真重复和A/B/A，不借UUID或诊断标签续命。现有limit单位/去重/重载不变。
4. 联合验收包含原生argv/code两种合法写法、helper不匹配负例、False/partial/blocked真实发射、恢复后的相同脚本不同合法外壳、非法env父和值、真重复及剩余额度主动停止。把候选随后交真实action审批/执行/证据/Reviewer/Commit和报告链的组合验收与本分册组件见证区分。

语言范围：Bash和Python实跑；R支持入口的静态适配已核，但本轮未调用R，不安装/探测正式科学能力。公开Skill helper脚本的当前import边界：WGS `inspect_fastq.py, inspect_fasta_header.py, inspect_fastq_batch.py, inspect_wgs_inputs.py`及TAD `check_cooler_resolution.py`都由脚本自身相对Skill安装根导入emit_observation；外部h5py/cooler未运行。WGS既有guarded tests可复用；TAD实际进程NOT_RUN。不能由共有名字断言不同环境均可用。

最终run04七项witness全部PASS，完整命令、失败run保留、实际加载/替身边界见`verification.json`与`final_export_manifest.json`。本分册不修改生产代码、旧测试断言、Skill或prompt，不提交Git，不push。审计witness PASS与FIX PASS分别报告；当前native helper/纠正仍有缺陷，真实模型和真实科学验证均未执行。

## 本轮harness限制（不隐去失败）

run01的自有测试函数名笔误与新发现env-list生产ValueError分别保存。run02六项witness通过。run03为增加真实小进程，曾把新合成pending argv中的`bash -lc`直接嵌套运行；父进程的Python audit guard不能继承至shell的文件读取，出现宿主集群登录profile banner，使“stdout为空”断言失败。该子shell间接读取哪些配置/凭据为UNKNOWN，不能声称已证明未读.env。没有主动读取秘密文件、原批准脚本或正式科学输入；没有真实provider/API/科学工具调用。run03原记录不覆盖，后续run04只执行相同已知合成body、使用现有registered nonlogin shell，小进程不再运行`-lc`。这种变更只收紧本轮测试启动方式，生产源码未改。原事故approved.sh:1本身也保留bash -lc，但不能用本次harness日志推断历史profile读取细节。完整shell后代文件/网络约束仍是现有guard的限制；总报告须保留。

## W7补充：当前post-await取消保护已验证，无需重做

`test_incident_late_result.py`的两个当前原生graph场景使用相同正式Plan前置和合法native JSON，由双Event固定在provider await中。取消场景只调用真实`_commit_user_cancellation`，非语义正例只用`save_plan_state`保存新诊断note，均未写success/ready权威状态。结果见`late_result.json`与`late_export_manifest.json`。

取消使durable status=cancelled，`validate_plan_for_generate`的terminal检查导致`approval_applicability`由applicable变unverifiable；post-await `evaluate_node_staleness`产生`stale_result(changed_preconditions=[approval_applicability])`。真实条件路由先到Planner，Planner真实terminal guard给cancelled并END，模型Planner调用总计仍1（只有最初计划）。无pending action、额外审批或attempt。晚到provider claim被记录completed仅代表内容归档，未取得执行资格。非语义note使state_generation20→21，但Task/Plan/Skill/approval语义均不变，因此正常得到action_ready与execute路由；测试在真实Execute前停止，执行0。两用例全PASS。该验证不覆盖取消前dispatch或保存锁前全部窗口，不扩大成所有取消已证明。

IG-D4补强见`progress_refinement.json`：只增量重跑repair case，两个公开业务对象除新增action_type=execute外其余字段完全相同，代码hash与非法env值保持不变。真实parser错误由缺action_type转为env值类型错误；两个完整候选指纹不同，但malformed-candidate wrapper的顶层绑定事实均只有binding_env，仍remaining3误停。该明确表示纠正没有引入新的错误；新witness PASS，不是生产修复。
