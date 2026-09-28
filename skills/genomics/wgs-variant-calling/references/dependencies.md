# 依赖与资源

此文件列出方法所需的软件事实，不是可执行 runtime、安装授权或本机环境登记。
不要求固定 Conda 环境名或作者目录；已有可用环境直接复用。缺失软件须先与用户确认。

依赖按正文真实命令和所调用脚本的 import/library 列表确认；未声明最低版本，不推断已安装。

## 版本与执行
原资料没有明确限制的依赖，不在此推断最低或唯一支持版本。加载本技能不执行 healthcheck。
命令示例中的参数、阈值及输入列以正文为准；环境名称属于运行机器，不属于方法。

## 原方法输出

- {'output_id': 'interval_plan', 'default_filename': 'intervals.tsv', 'format': 'tsv'}
- {'output_id': 'cohort_vcf', 'default_filename': 'cohort.vcf.gz', 'format': 'vcf_gz'}
- {'output_id': 'filtered_snp_vcf', 'default_filename': 'filtered.snp.vcf.gz', 'format': 'vcf_gz'}
- {'output_id': 'pca_results', 'default_filename': 'cohort.eigenvec', 'format': 'tsv'}
- {'output_id': 'snp_counts', 'default_filename': 'snp_counts.tsv', 'format': 'tsv'}
- {'output_id': 'report', 'default_filename': 'report.md', 'format': 'markdown'}
- {'output_id': 'reproduce_script', 'default_filename': 'reproduce.sh', 'format': 'bash'}

