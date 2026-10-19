# 历史证据说明

当前交付结果以 [FINAL_CHECK.md](FINAL_CHECK.md) 和 [final-results.json](final-results.json) 为准。本目录保留少量历史结构化记录，用于追溯真实模型验证和功能演变；历史测试数量、旧路径与外部审批状态均不代表本次重新执行。

| 文件 | 保留目的 |
|---|---|
| acceptance.json | 早期两个 Agent 的离线矩阵与三个真实模型案例 |
| verification.json | CLI/Web 工程验收、真实模型案例和当时的新虚拟环境安装记录 |
| quality-review-s8.json、quality-review-s8-trace.json | 质量审查 Q01–Q20 矩阵和受控反馈证据，历史全量 1029 项 |
| agent-layout-20261019.json | 三 Agent 并列迁移后的 1037 项回归与单包安装验证 |
| quality-demo-trace.json、quality-review-demo.html | 当前可重生成的固定响应二轮审查演示；真实 pytest，零模型请求 |

旧阶段计划、一次性阶段脚本、重复 Markdown 报告、旧 JUnit 和过期的 S9 待执行清单已清理。真实模型质量审查效果尚未联网验收，不以离线演示或较早的两 Agent 真实案例替代。保留的历史 JSON 内部可能指向本地 outputs 或旧目录，不作为跨机器复现命令。最终验收另外提供当前 JUnit、摘要和可重新生成的演示。来源审计三份 JSON 必须与源码一起保留。
