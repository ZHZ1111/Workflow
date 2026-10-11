# 节点协议与状态提交

版本：1.0（P4 兼容增加）/ 2026-10-10。代码以 `src/test_workflow/` 为准，Pydantic 校验拒绝额外字段；JSON 保存采用旧 `ContractModel.canonical_json()`，恢复采用 `model_validate_json()`，避免 computed_field 反向输入错误。

## 接口与顺序定义

`Agent.run(context: AgentContext) -> AgentResult` 是同步接口。注册表将稳定标识映射到节点实例，同名注册和缺失实现会被拒绝。`WorkflowDefinition.nodes` 声明节点顺序和 `receives_from`；接收源必须已出现。P1 固定执行首轮顺序链路，没有条件分支或反馈重入。

```python
registry.register("test_generation", FakeGenerationAgent())
registry.register("test_execution", FakeExecutionAgent())
runner = WorkflowRunner(registry, DEMO_WORKFLOW)
final = runner.run(initial_state)
```

增加一个不依赖消息的节点只需实现接口、注册并追加 NodeDefinition；自动测试已验证这一点。依赖前置消息的节点还须在发送节点输出中声明对应 receiver。业务节点不创建或调用其他节点。

## 数据契约

| 模型 | 内容与限制 |
|---|---|
| WorkflowConfig | mode、max_rounds 1–3、执行超时 1–300 秒、可选 ModelInfo；拒绝 Key 和 headers |
| AgentContext | run_id、目标 round、agent_id、绝对 run_dir、snapshot_json、inbox；元数据必须与快照一致 |
| AgentResult | 标识、轮次、状态、StatePatch、消息、产物；error 必须有 error_code 且不得夹带成功输出 |
| StatePatch | 单个 CandidateRound、追加 issues、next_action、API 调用增量、TokenUsage；无任务标识、预算、路径或历史整体覆盖字段 |
| Message | 版本、唯一 message_id、run_id、来源 round、sender、receiver、type、payload |
| MessagePayload | candidate_id、ArtifactRef、reason、可选 recommended_action；P1 不伪造实测反馈 |
| ArtifactRef | 任务内相对 path、kind、SHA-256、来源 round、candidate_id |
| WorkflowState | 编排状态、可空业务结论、轮次/当前节点、公开配置、AgentState、消息、消费标识、产物、事件序号、时间与错误码 |

公开配置构造样例：

```json
{"mode":"demo","max_rounds":3,"execution_timeout_seconds":30,"model":null}
```

P1 demo 子命令使用该默认配置。P4 `run --demo` 使用 mode=offline，真实 `run` 使用 mode=real 并通过 --env-file 配置模型；离线模式不读取配置文件。模型为空时，旧 TaskInput 使用明确的离线模型标识以满足旧 Schema；这不是一次模型调用。

候选唯一标识为 `<run_id>:<round>`。消息的 round 代表来源轮次；未来反馈生成的目标轮次由编排器另行指定。消息文件采用 POSIX 形式的相对路径，拒绝绝对路径、盘符、反斜杠、`..` 和 Windows ADS；解析后再检查真实路径归属与摘要，能拒绝向任务外的 junction 引用。

Context 本身冻结，业务状态通过 JSON 独立解码。节点可以在本地副本上计算，但该副本不会修改 Workflow 的共享状态，也不会影响下一次 `context.state`。任务目录引用不构成文件系统权限隔离，节点仍须遵守产物目录约定。

## 编排器提交规则

1. 保存运行进度，然后将快照与尚未消费的接收消息交给节点。
2. 重新校验 Result Schema，检查任务、节点、轮次与产物归属；核对文件 SHA-256。
3. 校验 receiver 是流程中声明的消费方，拒绝重复 message_id 与未登记文件引用。
4. 新候选只能按连续轮次追加；评估可更新当前候选的执行/coverage，不能改测试内容、路径或更早历史，已有执行不可再覆盖。
5. `candidate_ready` 的候选文件必须与状态补丁中的 CandidateRound 相同；消费前再次校验文件摘要。
6. 全部校验通过后写消息、事件和新状态；消费标识随成功结果提交。失败时停止后续节点，保留已提交产物。

P1 假执行节点进一步验证测试文件内容等于候选中的生成代码，并保存 `DemoObservation`。该观察明确含 `model_called=false`、`pytest_executed=false`，不使用 ExecutionResult 或 CoverageResult 伪造实测指标。演示完成时 `workflow_status=finished`、`task_status=null`。

## 持久化与日志边界

状态先在同目录临时文件写入、flush/fsync，再用 `os.replace` 替换；替换失败清理临时文件并保留旧快照。事件每行一个 JSON 对象，seq 从 1 连续递增，重新创建写入器时校验已有任务归属与序号。消息另存 `messages.jsonl`，state 保存同一批消息及消费标识，供检查交接。

事件只允许固定公开字段，异常只记录受控错误码，不保存异常原文、认证头或客户端配置。消息只接受公开的类型化负载，节点不得将凭据放入原因文字。P1 不读取环境密钥。输入源码与规格保存在状态快照中，原始运行目录仍属于本地证据。

单进程、单写入者是当前假设。JSONL 与状态替换不是跨文件事务，异常终止可能令日志领先于快照；`inspect` 仅校验读取，不进行消息回放或断点恢复。若磁盘写入持续失败，调用方会收到异常，最后一次成功状态仍可读取，不能保证保存新的错误状态。

## 可校验样例

`examples/contracts/p1/` 保存生成/执行 Context 与 Result、交接 Message、最终 WorkflowState；`schemas/` 保存四个顶层契约的验证 Schema。运行 `scripts/p1_baseline.py` 会重新生成样例，再运行包括样例恢复校验在内的自动测试。

样例中的任务绝对路径是本次运行事实；它们不作为跨机器执行入口。文件交接使用 ArtifactRef 的相对路径，随实际 run_dir 解析。正式 Schema 来源是代码；P1/P2 样例保留历史版本，P3 当前契约和 Schema 位于 examples/contracts/p3/。

## P2 的兼容增加

ArtifactRef 增加 `agentdev_candidate`、`plan`、`llm_result`、`subject` 类型；AgentResult 增加可选 `generation` 遥测，错误结果也可携带该安全遥测，仍不得携带成功状态补丁或交接消息。StatePatch 增加 `token_usage_reported`，用于区分“没有用量更新”和“累计量无法完整获得、应置空”。P1 已保存示例仍可恢复；其 Schema 是 P1 历史产物，当前 Schema 另存 `examples/contracts/p2/schemas/`。

`WorkflowRunner.apply_result()` 是公开的提案校验入口，返回新快照而不写磁盘；协调者负责保存。P2 验收脚本使用它提交单步结果，不绕过历史与文件校验。生成节点只在候选通过门禁且没有阻塞疑问时发出 `candidate_ready`。无效候选在结果与文件中保留，但不交给旧假执行节点执行；修复、复核、停止分支的正式调度属于 P4。

## P3 的兼容增加

AgentResult 增加可空 assessment 和默认空 diagnostic_paths；MessagePayload 增加可空 feedback；ArtifactRef 增加 assessment、execution_evidence 种类。旧样例依靠默认值继续有效，schema_version 仍为 1.0。ExecutionAssessment 校验候选归属、一次工具调用、pytest 启动状态与真实观察一致、建议映射；completed 建议要求全通过、目标覆盖达标、没有完整性错误或问题。

ExecutionFeedback 直接复用原执行/覆盖模型，保存校验错误、issues、protected_tests 和独立序列化的 source_hints_json。没有执行不能有覆盖率，校验拒绝不能附执行。错误 Result 没有 patch/messages/artifacts/assessment 成功输出，但可提供任务内相对诊断目录。

EVALUATION_WORKFLOW.feedback_routes 声明执行→生成的反馈边，不能连接未知节点、自连接或重复声明。apply_result 校验 assessment 文件、所有实测证据和消息负载一致，执行观察只能更新当前候选，不允许改写生成内容或重复执行历史。P3 协调验收脚本仍手动调用节点；P4 的 TESTING_WORKFLOW 使用同一反馈边，通过 run() 实现自动调度与最终状态提交。

执行接口与错误约定详见 [execution.md](execution.md)。

## P4 的兼容增加

WorkflowConfig.mode 增加 offline，表示离线生成、真实测试执行；demo 继续表示 P1 假节点，不能提交业务结论。测量模式的终止状态要求 task_status 和一致的 stop_reason；completed 要求最新候选全通过及覆盖达标。Workflow 的 selected_round 与 AgentState.best_available_round 同步。

Event 增加 node_failed、route_selected，以及可空 duration_seconds、action、stop_reason；旧事件依靠默认值继续有效。每次节点调用在 node_results/NN/<agent>/ 保存 Context/Result，评估建议经过 Workflow 校验后才决定循环或停止。

当前契约示例与 Schema 在 examples/contracts/p4/，包括完成任务、两轮反馈、后轮执行错误的历史选择状态，以及第二轮生成 Context 和其来源反馈消息。P1–P3 的契约样例保留为历史证据。详见 [workflow.md](workflow.md)。

## P6 的兼容增加

Event 增加 node_output_saved、message_consumed、workflow_stopped；增加可空 candidate_id、context_path、result_path、artifact_path，路径必须是任务内相对路径。旧事件仍使用 schema_version=1.0，可依默认值恢复；本次 Event Schema 和实测消息、Result、事件在 examples/contracts/p6/。

message_consumed 只在接收节点有效提案后记录，其 round 是接收节点目标轮次，candidate_id/artifact_path 仍指向消息来源候选。messages.jsonl 无消息时为空文件。发送/消费事件不承诺跨文件事务或分布式 exactly-once，state.consumed_message_ids 是已提交快照中的消费记录。

ResultSummaryNode 返回空 StatePatch、无消息、单个 kind=report 的 ArtifactRef，使用已有提案/摘要校验。摘要 JSON 的 phase 明确是业务停止、最终导出前；节点不可修改共享状态。summary.json 另使用导出 schema_version=1.1，保留 P4 键并增加逐轮/节点/消息/路由指标，不改 WorkflowState 或 AgentDev 模型。只读检查详见 [reporting.md](reporting.md)。
