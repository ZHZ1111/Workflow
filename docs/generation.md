# P2 单步生成与修订

日期：2026-10-09。入口为 `TestGenerationAgent.run(AgentContext) -> AgentResult`，生成客户端由构造参数注入；节点不创建其他 Agent，不执行 pytest，不调用旧 run_agent 或 run_initial_step。

## 入口和一次调用

```python
from pathlib import Path
from test_workflow.agents.test_generation import TestGenerationAgent
from test_workflow.contracts import AgentContext
from test_workflow.demo.client import FixtureClient
from test_workflow.workflow import create_run

state = create_run(source=Path("examples/calculation.py"),
                   specification=Path("examples/calculation.md"), output=Path("outputs/p2"))
state = state.updated(workflow_status="running", current_node="test_generation", current_round=1)
context = AgentContext(run_id=state.run_id, round=1, agent_id="test_generation",
                       run_dir=state.run_dir, snapshot_json=state.canonical_json())
agent = TestGenerationAgent(FixtureClient((Path("examples/fixtures/calculation_complete.json"),)),
                            client_mode="offline")
result = agent.run(context)
```

节点只返回提案，调用方通过 `WorkflowRunner.apply_result()` 验证后提交状态。完整离线案例与保存过程见 `scripts/p2_baseline.py`。注入 `DeepSeekClient` 时，client_mode 必须匹配真实、MockTransport 或 offline 场景；不会自动读取 `.env`。

首轮调用前要求源码分析与规格已存在、历史为空、内层工作区不存在。修订目标必须是连续历史的下一轮且不超过 1–3 轮预算。先检查源码副本、历史计划/测试/文件摘要、下一轮路径与交接路径，避免对已有或污染目录消耗模型请求。

## 修订反馈

Context.inbox 必须有一条已登记且未消费的 execution_feedback。sender 为 test_execution 或门禁协调者 workflow；receiver 为 test_generation；来源轮次、candidate_id 和文件摘要对应最近候选。建议动作只能是 repair_generated_test 或 add_missing_cases，并与状态 next_action 和旧 choose_action() 基于证据的判断一致。已达标、复核、超时、停止或预算耗尽的状态不能被一条修订消息推翻。

通过校验后复用 build_action_context()：保留行为规格、失败位置、覆盖缺口、真实调用证据、历史、已通过测试清单与剩余预算。实际交接消息追加在 WORKFLOW_HANDOFF_DATA 数据区；其文字不作为宿主指令。每次只调用一次客户端，再调用 stage_revision_candidate() 保存一轮；下一轮何时发生仍由外层协调者决定。

## 候选与产物

| 路径 | 内容 |
|---|---|
| agent_workspace/subject.py | 旧保存入口创建的原编码源码副本 |
| agent_workspace/rounds/NN/plan.json | 原测试计划 |
| agent_workspace/rounds/NN/llm_result.json | 原客户端结构化结果、重试/修复/用量 |
| agent_workspace/rounds/NN/candidate.json | 原 CandidateRound，test_path 为内层相对路径 |
| agent_workspace/rounds/NN/test_subject.py | 仅门禁通过且有代码时保存 |
| handoffs/NN/candidate.json | Workflow CandidateRound，test_path 转为绝对路径 |
| node_results/NN/context.json, result.json | 验收协调脚本保存的节点输入输出 |

后两项位于外层，不影响旧首轮工作区约束。ArtifactRef 使用任务相对 POSIX 路径；共享 subject 只在首轮注册，避免修订重写引用。原 vendor JSON 和源码均不修改。

语法、映射、预期模式、历史保护等候选门禁失败仍返回 `status=ok`：表示这次生成动作完成并有明确拒绝证据，不表示测试可执行。candidate.validation_error 保留原因，test_path 为空，不发 candidate_ready。阻塞规格疑问建议 request_review，也不发执行交接。节点不自动删除历史、降低断言或创建空测试来伪装成功。

## 错误、预算与计数

| 错误类别 | 处理 |
|---|---|
| transient | 网络、超时、限流或服务错误；旧客户端有限重试后返回失败，节点不再重试 |
| authentication | 鉴权、权限、余额；返回明确 provider_code/status_code |
| configuration | 本地配置、无效请求或模型/服务不存在 |
| format | 结构化输出或响应格式失败；旧格式修复预算耗尽后停止 |
| workspace | 目录已存在、源码/历史变化、越界、读写失败 |
| budget/contract/internal | 轮次不合法、建议不一致、客户端违约或未知异常；无成功补丁 |

错误结果保留 GenerationTelemetry，不保存异常原文、Key、认证头或服务响应正文。format_repairs 和失败用量若旧 LLMError 未提供，记录 null；未知请求次数也记录 null，不假装没有请求。

client_calls 每次节点最多 1；request_attempts 是客户端报告的尝试数，可能包含 HTTP 暂态重试和格式修复。external_requests 在 offline/mock 模式为 0，real 模式等于可得的请求尝试数；失败不可得时为 null。新 Workflow 的 api_calls_delta 计真实外部尝试，与 P0 离线旧适配器的模拟尝试计数分开。

错误结果不提交成功状态补丁，其尝试数保留在 generation 遥测中，供后续协调器汇总；不能仅根据成功候选的 api_calls_delta 计算包含失败请求的全部费用。

DeepSeekClient 默认请求上限为 5（首请求 + 最多 2 次暂态重试 + 最多 2 次格式修复），可注入旧有限 RetryPolicy。声明的客户端上限须落在动作预算内。未知自定义客户端须自行保证有限请求；适配器仅调用它一次并检查返回的尝试数，无法在它内部抢占 HTTP 请求。

成功时 total_usage 是本次可得量，StatePatch.token_usage 是累计可得量；有一轮缺失则累计量相应不可得。token_usage_reported=true 可明确清空未知累计量，避免保留旧已知值冒充完整总量。

P2 只完成生成能力的离线与本地 HTTP 模拟验证。实际执行/归因 Agent、自动反馈、最终业务结论、真实模型验收分别属于 P3、P4、P5。

## S5 质量补充（2026-10-16）

`adapters/quality_revision.py` 新增 validate_quality_revision 与 build_quality_revision_context。开启质量审查的下一轮，可接收上一轮审查节点发出的唯一已提交 quality_feedback；前轮必须仍有真实通过、覆盖达标的执行记录，原 choose_action=FINISH/completed/coverage_target_met 在该分支是必要条件。预算、源文件/规格、历史产物、审查记录、响应、反馈负载和真实执行证据均在调用客户端前核验。关闭开关、批准或人工复核、错误、已消费/混合消息或修改后的证据不能触发质量生成。

质量分支直接复用 build_feedback_context，保留源码、规格、完整前轮候选、实测执行/覆盖和预算。统一 TASK_DATA_JSON 数据区的 task_kind 改为 quality_extension，增加实际质量消息、QualityAssessment、quality_history、最新未解决阻塞发现、全轮历史摘要及 protected_test_functions。审查内容为不可信分析数据；预期仍以原行为规格为准，不能根据模型意见改写规格或已通过断言。

质量分支不调用 build_action_context，旧执行反馈的动作一致性校验、上下文与首轮行为保留。生成客户端仍每节点只调用一次；返回的完整候选仍由 stage_revision_candidate 验证并保存，自动执行和消息消费归外层 Workflow。删除或弱化已通过测试、修改其装饰器/映射/场景/支持代码的候选不发出 candidate_ready。

S5 的固定客户端验收已接入真实 TestGenerationAgent、TestExecutionAgent 和 TestQualityReviewAgent，而非 S4 的受控生成节点；共享服务、CLI/Web 开关尚待 S6。实现及验证见 [S5 记录](quality-review-s5.md)。
