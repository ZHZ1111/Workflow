# 增加一个节点

版本：P6 / 2026-10-11。`ResultSummaryNode` 是正式交付的无模型扩展节点，位于 `src/test_workflow/nodes/result_summary.py`。它证明统一接口、注册表和流程定义可以扩展固定两 Agent 工作流，不计作第三个 Agent。

## 运行示例

```powershell
.\.venv\Scripts\python.exe -m test_workflow run --demo --with-summary --source examples/validation.py --spec examples/validation.md
```

离线生成、真实 pytest/coverage；validation 的确定性候选产生两轮候选和一次执行→生成反馈。CLI 打印任务目录。在第二轮停止结论提交后，尾部节点读取 `context.state`，保存 `extensions/02/result_summary.json`，返回一个有 SHA-256 的 `report` 产物引用，然后 Workflow 校验并提交该引用，最后统一导出报告。默认 `run` 保持两个生产 Agent，`--with-summary` 才加入这个节点；真实模式同样可注册，但本次 P6 仅用离线生成验证。

扩展 JSON 明确写 `phase=business_conclusion_before_export`，当时编排仍为 running，task_status/stop_reason 已确定；这不是最终完成状态。节点不调用模型、不启动 pytest、不发消息、不修改候选/问题/建议或最终选择。没有执行候选时也可以保存 failed 的摘要，节点异常时按统一错误处理停止。

## 三步接入

1. 实现同步 `run(context: AgentContext) -> AgentResult`。通过独立快照读取共享状态；只向 `context.run_dir` 写新文件，返回受限 StatePatch、消息或 ArtifactRef，不直接保存 WorkflowState。
2. 以稳定标识注册实例。
3. 将 NodeDefinition 追加到流程定义，在报告导出前调用。

```python
from test_workflow.definitions import TESTING_WORKFLOW, NodeDefinition
from test_workflow.nodes.result_summary import ResultSummaryNode
from test_workflow.workflow import WorkflowRunner

# registry 已注册两个生产 Agent，initial 是 create_run() 的新任务。
registry.register("result_summary", ResultSummaryNode())
definition = TESTING_WORKFLOW.updated(
    name="testing_with_summary",
    nodes=(*TESTING_WORKFLOW.nodes, NodeDefinition(agent_id="result_summary")),
)
final = WorkflowRunner(registry, definition).run(initial)
```

实际 CLI 使用同样的 `SUMMARY_WORKFLOW` 常量。主循环已有通用尾部节点调度；这次没有加入 `result_summary` 业务特例。产物的 `round` / `candidate_id` 归属节点执行轮次；JSON 内的 `selected_round` 可以是更早轮次，二者含义不同。

当前定义支持两 Agent 有限反馈后追加读取共享快照的尾部节点。声明 `receives_from` 则必须有相应的实际消息交接，不能仅声明依赖却不发送消息。它不是任意 DAG/并行调度器，两个核心节点仍按固定语义产生候选和评估。

## 将来接入评审 Agent

可以沿用统一接口注册评审 Agent，并显式扩展严格消息/结果契约、允许的路由及预算，验证其反馈与候选归属后再参与决策。当前尾部节点只能观察已经确定的结论；若要评审 Agent 改变 completed/needs_review 或重新开始生成，需要增加并验证正式状态转换，而不能让扩展节点直接覆盖历史或共享状态。本阶段不实现新的模型角色。

本次两轮任务的反馈消息、扩展 Result、事件及 Event Schema 在 `examples/contracts/p6/`；文件中的 run_id 和绝对反馈证据路径是本次运行事实，不能用作跨机器启动参数。验收见 [../evaluation/p6.md](../evaluation/p6.md)，报告格式见 [reporting.md](reporting.md)。
