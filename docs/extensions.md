# 扩展节点与流程

Agent 统一实现同步 `run(context: AgentContext) -> AgentResult`，注册表按稳定标识查找实例，流程定义声明执行顺序和反馈边。生产代码见 [base.py](../src/test_workflow/agents/base.py)、[registry.py](../src/test_workflow/registry.py)、[definitions.py](../src/test_workflow/definitions.py)。

## 已实现的扩展

质量审查 Agent 是正式的第三个 Agent，可在 CLI 用 `--with-quality-review` 或 Web 复选框启用，默认关闭。它能通过受控反馈影响下一轮生成和最终结论；相关质量契约、状态、校验和路由均已加入，不能用一个普通尾部节点冒充同等能力。

ResultSummaryNode 是无模型尾部节点，通过 `--with-summary` 开启，与质量审查可组合。它读取已确定的业务结论、保存摘要并返回产物引用，不改候选、反馈、最终选择或业务结论；它不计为第四个 Agent。

```powershell
.\.venv\Scripts\python.exe -m test_workflow run --demo --with-summary --source examples/validation.py --spec examples/validation.md
```

## 添加观察型尾部节点

1. 实现 run 方法，从 context.state 快照读取，只向当前任务的新文件写产物。
2. 返回合法 AgentResult 和任务内相对 ArtifactRef，不直接保存 WorkflowState。
3. 使用唯一标识注册实例，在现有定义末尾追加 NodeDefinition。
4. 验证成功、业务失败和节点错误时的行为，以及产物归属和摘要完整性。

```python
from test_workflow.definitions import TESTING_WORKFLOW, NodeDefinition
from test_workflow.nodes.result_summary import ResultSummaryNode
from test_workflow.workflow import WorkflowRunner

# registry 已注册两个生产 Agent；initial 是 create_run() 创建的新任务。
registry.register("result_summary", ResultSummaryNode())
definition = TESTING_WORKFLOW.updated(
    name="testing_with_summary",
    nodes=(*TESTING_WORKFLOW.nodes, NodeDefinition(agent_id="result_summary")),
)
final = WorkflowRunner(registry, definition).run(initial)
```

示例使用正式 SUMMARY_WORKFLOW 的相同机制，主循环没有摘要节点专用业务分支。尾部节点在业务结论确定、最终报告导出之前运行，快照中的 workflow_status 此时可以仍为 running。

## 增加会改变结论的新 Agent

除注册节点外，还须设计严格输入输出和证据契约，扩展状态与受限 StatePatch，声明消息与路由，验证反馈归属和消费时机，定义预算/无进展/错误停止条件，再同步报告、CLI/Web 和测试。质量审查就是这一类扩展的现有例子。

当前核心生成/执行语义和质量审查插入点明确固定；支持受约束的顺序扩展与有限反馈，不是任意 DAG 或并行调度器。声明 receives_from 必须有真实合法消息，不能只声明依赖而不发送交接。
