# B：请求绑定、启动和补登记

41 项 B 目标回归通过（19 候选/有界定位、10 Domain、9 共享执行、3 真实 Pi→Domain），32 项 A 回归通过。证据见同目录日志。

继续使用 RegistryView 权威路径；坏库/缺库/不可读/不适用与未安装分开。能力候选支持精确约束、歧义和未知事实。已选事实存入既有 runtime_evidence acceptance，含原 evidence_id、request_scope、运行时 scope 与启动描述摘要；不建第二个绑定库。普通选择禁止子进程。

run_command 使用显式目标/ref 或本请求唯一适用绑定；多 scope 需指定。Pi 同名 bash 增可选 binding_ref，裸调用保留宿主shell、设置、管道、超时、输出；带ref逐次调用Python共享启动解释。runner保持argv边界，不拼接任意语言命令，entry-only无runner shell明确unsupported。prefix shell仅承诺PATH优先，不能把任意shell中所有工具都说成来自prefix；精确argv与安装检查证明实际entry。

补登记为纯metadata，模型声明不能升格为验证，重复无变化不改证据时间。独立工具表已有同名工具绑定其他环境时保持旧表并返回partial；准确新环境记录仍可按自己的entries选择。Skill映射未被强制改写。protected注册仍需原写权限；prefix不是目录授权，执行必须满足host原runtime根或当前live路径授权。

未命中定位复用现有executor，固定只读metadata程序、最多3候选、单段5秒/总15秒/8KiB摘要；不运行候选工具、不启动module/login、不递归扫描。同请求重复定位使用已有结果。清理事实按executor回执报告，正式分析不受定位短预算影响。

生产 rna_env 已通过新host补登记入口写到 /public/home/liusiqi/03.毕设/agent_shim/software_registry.json；只读历史会话回执和准确路径metadata。证据时间2026-10-01T08:26:16.342Z；fgsea版本未知。新进程/临时新会话仅查询AVAILABLE且零子进程。未重新运行R/fgsea、TAD、ORA或reproduce。相关前后记录、权限路径与回滚脚本见 rna-registration.json、supplement_rna.py；其余记录精确不变。

Git delta处分见deltas/B-staging.json。可直接应用旧HEAD的本轮块进入源码；与既有dirty冲突的块提交精确补丁，运行文件留在有效树。构建使用已有compiler，1024MB进程被环境杀死后，以768MB堆上限完整构建成功；未安装依赖。源码和dist散列见build-B.json。
