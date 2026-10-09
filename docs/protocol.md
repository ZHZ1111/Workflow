# P1 节点协议与状态提交

版本：1.0 / 2026-10-09。代码以 `src/test_workflow/` 为准，Pydantic 校验拒绝额外字段；JSON 保存采用旧 `ContractModel.canonical_json()`，恢复采用 `model_validate_json()`，避免 computed_field 反向输入错误。

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

当前 CLI 使用该默认配置，未提供配置文件读取选项。模型为空时，旧 TaskInput 使用明确的离线模型标识以满足旧 Schema；这不是一次模型调用。

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

样例中的任务绝对路径是本次运行事实；它们不作为跨机器执行入口。文件交接使用 ArtifactRef 的相对路径，随实际 run_dir 解析。现阶段正式 Schema 来源是代码，P3/P4 扩充反馈负载时应同步升级样例与文档。
