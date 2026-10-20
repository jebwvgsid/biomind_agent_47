四道原题输入修订，20261020-fullanalysis。

cases/ 保存已实际生效的四题题面、输入清单和可配置训练入口。inputs/ 保存本次生成的输入、映射和来源说明；external-assets.json 记录原始共享大文件的工作区相对位置、长度和 SHA256。它们留在原位，未重新提交或修改。运行 python tools/hydrate_external_assets.py --workspace <原工作区> 可核对并建立本地链接。
manifest.json 和题面仍使用该集群的真实绝对路径，是部署快照，不声称任意机器开箱即用。原始数据、旧题面备份、运行结果及评分均保留在原工作区；validation/ 仅保存本轮验收摘要，不含历史评分或模型权重。完整原始来源、训练检查点和日志位于 BioMind_Original_Cases_Revision_20261020。

本目录不属于 BioMind 核心运行时构建清单。生产 CLI 发布身份及本轮部署结果见 docs/implementation/submission-20261020/。
