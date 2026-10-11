# 运行轨迹与报告

版本：P6 / 2026-10-11。正式 `run` 无论正常结束还是节点错误，都尽量保存 `summary.json` 和 `report.md`；写入失败向调用方报错，不能保证失败磁盘上仍能保存新状态。

## 从结论追溯证据

打开任务目录中的 `report.md`。先看编排状态、测试结论和停止原因，再看选中与最新候选是否全部通过。报告中的节点输入/输出、逐轮测试、评估、执行日志、JUnit、覆盖及最终文件均使用任务内相对链接。

| 需要确认的事实 | 记录位置 |
|---|---|
| 两个 Agent 的职责和实际调用次数 | 报告“协作与节点调用”；摘要 `agents` / `node_invocations` |
| 某次节点收到哪些消息及历史 | `node_results/NN/<node>/context.json` |
| 节点返回了什么，是否提交 | 同目录 `result.json`；`node_finished` / `node_failed` |
| 某轮 pytest 实际执行了多少 | `round_details[].execution`；原始 execution.json / pytest.xml / pytest_result.json |
| 覆盖是否可用及缺口 | `round_details[].coverage`；原始 coverage_result.json / coverage.json |
| 修订依据和保护测试 | `evaluations/NN/assessment.json`、execution_feedback 消息及下一轮 Context |
| 编排采用哪个建议及为何停止 | `route_selected`、`workflow_stopped` 或 `workflow_error` |
| 最终导出的候选 | `selected_round`、`final/test_subject.py` 摘要及原轮次测试文件 |

`summary.json` 的导出契约版本为 1.1，保留 P4 已有键，增加逐轮实测、节点、消息、路由、耗时、目标阈值和已知用量。它是报告用 JSON，不是可送回 WorkflowState 校验的状态模型。原始业务模型继续使用 AgentDev 的冻结契约；生成和执行信息不从控制台文字猜测。

节点调用数根据已保存的 Context 计数，提交数根据对应结束事件计数；没有返回 Result 的节点单独列出。生成客户端调用、HTTP 请求、执行工具调用和 pytest 启动分别计数，客户端格式修复不等于新的 Workflow 轮次。无法取得的请求数、用量、测试计数保留 null。

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
