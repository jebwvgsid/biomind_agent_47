# 累计验收与边界

最终四批均使用规范源码清单 `9b4ebef7e58cbfc7fcfeaf6da17161d2fad21f604e1ac5fadbac24406e38c1dc`，每批 fresh interpreter，进程前后及最终主工作区均核对 926 文件。执行结果合计 **510 次测试执行：508 个不同 node，另 2 次同 node 的新进程续跑；45 个子测试通过**。这不是全仓测试全部通过。

| 批次 | 结果 | 秒 | 范围 |
| --- | --- | ---: | --- |
| runtime | 359 passed，26 subtests passed | 65.81 | binding 回滚/失效、安装授权/取消/目标、helper、runtime、既有恢复与范围保护 |
| graphs | 81 passed，19 subtests passed | 64.82 | Plan/审批、自动发现到 runner、S3 实际观察、Q 拒绝、S5 因果显示 |
| recovery | 68 passed，36 deselected | 99.50 | 最终派发预算、历史 seed、分段/多层合并、正式回读、Node epoch/CAS、持久化准备 |
| resume | 2 passed | 3.51 | 独立新进程读取活动 epoch、保持已消费额度、正式引用后续页 |

四批原始 argv、严格 env、状态和摘要见 [final_batches.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/final_batches.json)；原始日志、pytest event 结果及实际请求正文持久位置和 hash 见 [final_raw_evidence.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/final_raw_evidence.json)。每批 exit=0，SOURCE_UNCHANGED=True。各切片先前通过记录仅作过程依据，没有拼接计入此最终总数。

## 验证命令

实际入口是 `/public/home/liusiqi/anaconda3/envs/biomind/bin/python` 加累计候选的 `tests/offline_managed_bootstrap.py`，完整精确 selector 和 36 项排除直接保存在 final_batches.json。环境只传 PATH、独立 TMP/结果目录、BIOMIND_DISABLE_DOTENV 与 S4_RESTART_*。bootstrap 用 inert config 禁止配置凭据读取、网络/模型和安装，真实微小 runner 仅按已有测试白名单执行。

可用下列已交付脚本重建同一源码并复用四批原始参数；root 必须是尚不存在的 `/tmp` 子目录：

```bash
/public/home/liusiqi/anaconda3/envs/biomind/bin/python \
  '/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/reproduce_validation.py' \
  --root /tmp/biomind-gwas-s1-s5-recheck-20260918
```

这段重放不会启动原 session、真实主机探测、真实模型、安装或 Git 操作。脚本自身仅做文件重建和调用上述已实际通过的命令；未为文档再重复执行整套测试。最终归档源码的 Python 编译检查使用同一 biomind Python，通过；归档脚本曾误用系统 Python 3.9，遇到未改 WGS helper 的较新 f-string 语法后停止，仅调整归档运行解释器，未改无关 WGS 代码。

## S1—S3 自动链

S1/S2 用例由真实生产逻辑从默认环境缺失出发，读取限定 registry/Conda root，发现仅含当前 bcftools 能力的 bio_gatk。替身位于 provider `.ainvoke`、Inspector 最外 OS 探测叶子和人类审批决定；discovery、SkillManager、ledger、phase 新观察、最终请求、Plan/Generate、审批对象和 runner 都保留生产实现。没有手工预填候选/binding，没有用整套 EMMAX/R 要求提前排除当前阶段候选。

available 路径记录 Consultant/Plan/Generate 调用 3/1/1、probe 1、真实微小执行 1；refresh 路径为 5/1/1、probe 3、执行 1，证据计数 `[1,1,2]` 验证重复不追加、新事实 supersedes；设施失败路径 3/0/0、无执行/安装；审批后目标失效路径即使另有候选也不静默切换、不执行。scope 清理同时撤回 ledger 与请求中的旧可用表述。原始四场景记录在 graphs 的 gwas-*.json 中。

S3 联合图使用真实只读 helper、真实 runner、正式核验和 Commit，结构化 file_identity/样本/Q 观察进入下一实际 Generate 请求。对方返回依赖未知 Q 的动作时，真实 helper rc=2，在科学输出之前停止，只有初检 1 次 Commit。独立合成用例检查首样本、重复/缺失/错序、数值 Q 与带 hash 的完整显式行映射。空必需样本仍由正式核验拒收，rc=0 不升级为成功 Commit。

## S4 实际派发与恢复

最终适配器完整 messages/tools/model/max_tokens 的原始回执已经持久化；[FINAL_DISPATCH_BUDGETS.tsv](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/FINAL_DISPATCH_BUDGETS.tsv) 包含 **63 次**业务、分段、合并、CAS 竞争和新进程业务请求。逐条断言 `max_tokens = effective_output_limit_tokens = output_reserve_tokens`，输入不超过 safe_input，并满足输入 + 实际输出 + 估算余量 ≤ 对应配置容量。messages_sha256 只标识消息正文，完整 tools/model/上限保存在原始 JSON。表中 merge_level 从输入摘要的真实 level 加 1 推导，未改生产协议。

| 见证 | 实际结果 |
| --- | --- |
| 历史 seed 32,097 字符、27 个引用 | 完整输入估算 94,760 tokens，输出 8,192、margin 4,096、配置 200,000；到达业务 provider 1 次，compactor 0 次 |
| 合成材料增长 | 22 个 map + 第 1 层 merge 3 个 + 第 2 层 merge 1 个 + selector 1 个；随后业务请求 2 次，正式读证据并形成合法停止决定 |
| 压缩预算 | 测试将业务容量设为 350,000、压缩输入阈值 30,000，输出上限 4,096；这仅驱动有界集成，不修改真实全局配置 |
| Node 持久重入 | 活动 epoch 保持，压缩 27 次/已消费 634,177 tokens 不重置；新进程 1 次业务请求、0 次再压缩，工具回执及轮次状态延续 |
| CAS 竞争 | 真实持久保存造成 stale generation；27 次压缩后不激活失效候选，孤立候选可追踪，业务调用 0 次，无并行状态覆盖 |
| closure 与接收限制 | 最终 closure 实际上限 2,000；保持原 receive_char_limit，128 字符接收限制按 PROVIDER_OUTPUT_LIMIT 拒绝；超 2 MiB 外置单对象在解码前拒绝 |
| 正式读取 | 8 个既有对象有正文并可读；3 个原缺正文对象明确拒绝。首段/后段/新进程续读、错误作用域/revision/tool/digest、重复页不算新进展均覆盖 |

大型结果通过现有 PhaseArchive 登记和 evidence.read dispatcher 回读，按 120 行/16,000 字符有限分页，长 JSON 行按可逆块输出；读取页不再反复外置为新引用。Node 激活、maintenance/CAS、coverage 和已有额度机制未被替身替换；替身仅在最终模型传输叶子返回确定文本/工具请求。输入计量方法为本地 mixed_script_reference_fallback（ascii3-cjk1.25-other2-v1），不声称厂商 tokenizer 精确性或真实服务容量认证。

## 排除与未定异常

36 项 baseline 失败显式排除，未改历史测试预期伪装全绿。其中两个旧测试文件的 33 项在原冻结基线与本轮候选均为 35 passed/33 failed，失败节点和归一化 traceback 全部一致，新增失败为 0；其余 3 项沿用已确认旧 fixture/schema/写入守卫限制。精确名单见 final_batches.json，比较见 [recovery-baseline-comparison.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/recovery-baseline-comparison.json)、s5_legacy_baseline_results.json 等原回执。新 S4 测试另外通过真实生产 dispatcher 验证正式读取及拒绝规则，不以跳过旧测试替代功能验收。

此前同进程扩展运行在完成 359 个用例及 26 子测试后返回 `-11`，mapping[compact] 在 Generate 请求后没有 call/teardown 完成回执；日志没有 Python/native 故障栈，原因未确定。不能断言是 native 状态污染或本轮某项代码所致。最终按四个独立解释器执行通过，结论严格限于该执行方式。原始单进程与 baseline 对照日志已持久化，见 [diagnostic_raw_evidence.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/diagnostic_raw_evidence.json) 和 [single-process-exit-review.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/single-process-exit-review.json)。

## 真实主机与正式数据

另一次真实主机受控检查沿生产 bwrap 入口探测现存 bio_gatk 的 bcftools 版本/query 帮助，60 秒超时，结果 UNKNOWN；无 binding、无安装、注册表不变。没有绕过隔离。现有接口未带回进程 rc 或部分 stderr，记未知，不冒用历史 NETLINK 作为本次具体原因。实测入口与最终源码相关方法的 AST/依赖 hash 对照见 [host-controlled/final_entry_equivalence.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/host-controlled/final_entry_equivalence.json)。此项与替身 Inspector 的自动链验收分开。

正式允许输入只读观察：VCF 与无表头表型均 215 样本，首样本 GA0001，ID 集合和顺序一致；Q 215×3 无 ID，映射未知。见 [formal-inputs-final.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/s3-joint/formal-inputs-final.json)。没有启动关联、作图或候选区间分析，也没有恢复原 session 或改写其三份交付。

仍待后续：主机检查设施可用性、真实模型认证、确有需要且经授权的安装、Q 来源/映射、科学端到端验证；seed 字段精简继续作为可选后续。软件测试通过不代表当前主机自动 GWAS 已可运行或原 GWAS 已完成。
