# 运行轨迹与报告

版本：S7 / 2026-10-17（保留 P6 报告机制）。正式 `run` 无论正常结束还是节点错误，都尽量保存 `summary.json` 和 `report.md`；写入失败向调用方报错，不能保证失败磁盘上仍能保存新状态。

## 从结论追溯证据

打开任务目录中的 `report.md`。先看编排状态、测试结论和停止原因，再看选中与最新候选是否全部通过。报告中的节点输入/输出、逐轮测试、评估、执行日志、JUnit、覆盖及最终文件均使用任务内相对链接。

| 需要确认的事实 | 记录位置 |
|---|---|
| 生成、执行、审查 Agent 的职责和实际调用次数 | 报告“协作与节点调用”；摘要 `agents` / `node_invocations` |
| 某次节点收到哪些消息及历史 | `node_results/NN/<node>/context.json` |
| 节点返回了什么，是否提交 | 同目录 `result.json`；`node_finished` / `node_failed` |
| 某轮 pytest 实际执行了多少 | `round_details[].execution`；原始 execution.json / pytest.xml / pytest_result.json |
| 覆盖是否可用及缺口 | `round_details[].coverage`；原始 coverage_result.json / coverage.json |
| 修订依据和保护测试 | `evaluations/NN/assessment.json`、execution_feedback 消息及下一轮 Context |
| 编排采用哪个建议及为何停止 | `route_selected`、`workflow_stopped` 或 `workflow_error` |
| 最终导出的候选 | `selected_round`、`final/test_subject.py` 摘要及原轮次测试文件 |

`summary.json` 的导出契约版本为 1.2，保留 P4/P6 键及逐轮实测、节点、消息、路由、耗时、目标阈值；S7 将 `external_requests`、`token_usage` 从生成口径扩展为生成加审查的总量，分项见 `model_usage`。旧状态可重新生成摘要，但不会覆盖旧文件。它是报告用 JSON，不是可送回 WorkflowState 校验的状态模型。原始业务模型继续使用 AgentDev 的冻结契约；生成和执行信息不从控制台文字猜测。

节点调用数根据已保存的 Context 计数，提交数根据对应结束事件计数；没有返回 Result 的节点单独列出。生成/审查客户端调用、请求尝试、外部请求、执行工具调用和 pytest 启动分别计数，客户端格式修复不等于新的 Workflow 轮次。无法取得的请求数、用量、测试计数保留 null。

## 消息和事件的含义

事件使用递增 `seq` 和 UTC 时间。P6 增加 `node_output_saved`、`message_consumed`、`workflow_stopped`；可选路径字段只允许任务内 POSIX 相对路径，旧事件依靠默认值继续读取。

`node_started` 指向本次 Context；`node_output_saved` 表示 Result 文件已保存，**尚不表示提案有效**。Workflow 校验提案成功后，记录发送和消费事件，再保存 `node_finished` 与新快照。被拒绝的提案不消费输入消息。持续文件写入失败仍可能使日志领先于最后成功的状态，日志不是跨文件事务。

发送事件的 `round` 是消息来源轮次；消费事件的 `round` 是接收节点本次轮次。两条事件通过 `message_id`、`candidate_id`、`artifact_path` 关联同一份候选或评估。反馈引用同轮 assessment，该 assessment 又引用实际 pytest/coverage 证据；下一轮生成 Context 保存该反馈。

```text
第 1 轮 execution → message_sent：来源轮次 1，候选 run-…:1，assessment 01
第 2 轮 generation → message_consumed：目标轮次 2，同一消息/候选/assessment
```

`workflow_stopped` 记录已提交的业务停止结论，发生在可选尾部节点和最终导出之前；它不表示报告已保存。`workflow_finished` 在报告成功写入后记录。导出报告读取的是导出前证据，因此摘要中的事件序号可能比最终 state.json 少最后一个完成事件。`inspect --summary` 从最终快照重新构建只读摘要。

## 只读历史查看

```powershell
.\.venv\Scripts\python.exe -m test_workflow inspect <任务目录>
.\.venv\Scripts\python.exe -m test_workflow inspect <任务目录> --summary
```

第一条校验状态模型并打印状态。第二条还核对登记产物 SHA-256、事件序号/任务归属，以及存在的最终测试与源码副本是否匹配选中产物；已完成且选中候选的任务缺失最终文件也会失败。它不读取 `.env`、不调用节点/模型、不写文件、不续跑。损坏或缺失证据返回 2。P1 假节点演示没有实测业务结论；旧 P4/P5 任务可读取，但不会补造当时没有记录的消费事件或改写旧报告。

## 结论边界

`finished` 表示编排结束，`completed` 还要求最新候选真实全通过并达标。覆盖阈值沿用行 90%、有分支时分支 80%；无分支不是 0%。覆盖不可用保留 pytest 结果及错误原因；skipped/xfail/xpass 不视为全部通过。

后轮错误可保留较早可用测试，但报告仍显示最新错误和 partial。`needs_review` 保留规格支持的失败断言；测试没有运行不能报告“通过”。任务目录隔离不是执行安全沙箱。最终摘要扩展节点记录的是导出前的业务结论，不能代替最终 state.json。

## S7 质量结论与统计

各轮 `round_details[].quality` 保存状态/原因、QualityAssessment、response 路径、遥测与证据核验结果；根级 `quality_assessments` 只含已提交审查。报告展示规格原文、测试原文、目标函数、建议和人工问题，基础执行完成与质量批准分别记录。关闭为 disabled，基础未完成为 skipped；未送审、审查异常不能显示批准。

`selected_quality_approved`、`latest_quality_approved`、`exported_quality_approved` 分别对应选中、最新和实际导出候选，关闭审查时为 null。`unresolved_findings` 保留最近已提交审查的阻塞发现，`unresolved_findings_round` 标记来源，后轮未审查不会把旧发现清空。`messages` 记录来源及已提交的消费轮次/节点，不能把 Result 中未接受的消息提案当成实际交接。

`model_usage` 的 generation/review/total 各有 client_calls、request_attempts、external_requests、token_usage、known_*、client_modes 和 telemetry_complete。总量字段 `total_external_requests` 与 `external_requests` 相同。缺少遥测/Result 或身份不匹配时保留 null，已知小计不等于完整总量；未调用分项为零，缺失 total_tokens 不从其他 token 分量猜测。失败或被拒绝输出中身份匹配的遥测也计入观察用量。业务 `AgentState.api_calls/token_usage` 保持生成口径。

`inspect --summary` 除旧产物核验外，重新校验状态完成门禁、固定路径质量输出、规格/测试/执行引用、已提交节点决定与遥测，以及质量反馈与下一轮唯一生成 Context。任何不一致拒绝读取；普通报告构建将质量证据问题标为 error 并记录 `quality_integrity_errors`。旧任务缺省为未启用，不补造审查记录。具体实现及本阶段验证见 [S7 记录](quality-review-s7.md)。


## S9 材料与结果来源（2026-10-18）

产品摘要仍为 1.2，默认关闭时显示 disabled，基础未完成显示 skipped，未知请求/token 与未调用分开。实际选中、最新与导出候选的批准对象保持分别核对。生成加审查总量在 model_usage/根级 external_requests 与 token_usage 展示，业务 AgentState 保持生成口径。

`evaluation/quality-review-s9.json` 是独立验收索引，当前 real_model_executed=false、real_model_dispatches=0、real_quality_feedback_count=null；原因是自动审批拒绝外传启动。它不是 WorkflowState，也不是已完成的真实任务。离线演示索引另保存两轮执行与一次已消费质量反馈，HTML 的 45 秒只是录制讲解节奏，画面明确标注固定响应。真实任务若首轮批准，将如实记录零条真实质量反馈，不把离线补充移入真实统计。证据见 [S9 记录](quality-review-s9.md)。
