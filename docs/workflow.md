# 自动编排与有限反馈

版本：P6 / 2026-10-11。入口为 `test_workflow run`，`WorkflowRunner.run()` 是唯一调度入口。`--demo` 使用离线 FixtureClient，真实模式使用 DeepSeekClient；两种模式共用两个生产节点、真实执行工具和导出路径。默认使用 TESTING_WORKFLOW，`--with-summary` 选择 SUMMARY_WORKFLOW 并注册无模型尾部节点。P1 `demo` 子命令仍用假节点，不产生实测结论。

## 顺序与交接

创建任务前校验源码范围、非空 UTF-8 规格和公开预算，再进行 AST 分析。外层目录保存 Workflow 状态，内层 agent_workspace 由首轮生成创建。Python `create_run()` 默认在输出父目录下创建唯一 run_id 子目录；正式 CLI 显式 `--output` 使用 exact_output=True，拒绝任何已有目录或链接，避免覆盖历史。

每轮先调用 test_generation，再调用 test_execution。节点异常或 error Result 阻止后续依赖节点。无效候选仍可交给执行节点作评估，但不调用 pytest；该分支允许空 candidate_ready inbox，要求候选、门禁错误和文件证据已提交。候选包含规格疑问时也只评估并转人工复核。

Workflow 在节点开始前保存状态及 Context；返回后保存 Result，重新校验协议和文件，再提交提案、消费消息、记录耗时。异常原文不会进入事件日志，只保存固定错误码。成功和失败都有原始输入快照；error Result 可保留安全遥测和任务内诊断路径。

## 路由与预算

| 动作 / 原因 | Workflow 行为 |
|---|---|
| finish / coverage_target_met | 验证最新候选真实全通过且目标覆盖达标，提交 completed |
| repair_generated_test、add_missing_cases | 消费执行节点反馈，再调用生成节点；总候选预算 1–3，默认 3 |
| request_review | 提交 needs_review，保留失败断言、规格依据及问题 |
| finish / round_limit_reached | 有可用历史则 partial，否则 failed；不发起额外生成 |
| finish / no_progress | 重复有效候选在生成门禁停止；不同候选未增加已通过行/分支覆盖也停止 |
| finish / execution_timeout | 停止自动处理，保留真实超时观察，不启动下一轮 |
| finish / unrecoverable_error | 保留已有实测结果，例如 coverage 不可用；有可用候选则 partial |
| 节点异常、未知动作、不一致交接 | workflow_status=error，固定错误码，保留已提交历史 |

执行 Agent 提交 assessment 建议后，Workflow 对照复用的 choose_action() 校验允许的动作、停止原因和状态，再执行转换；完整性异常仅允许已知完整性错误的停止建议。轮次预算在 Workflow 和生成节点都受约束，LLM 的内部有限重试仍由原客户端负责，Workflow 不增加重试。无进展规则复用旧策略，历史通过函数、装饰器、场景与映射继续由 stage_revision_candidate() 保护。

反馈 round 是来源轮次。第 N+1 轮生成必须收到第 N 轮 test_execution 的唯一未消费 execution_feedback，并核对摘要；消息包含 execution、coverage、校验错误、protected_tests 和缺口提示，生成 prompt 实际包含这条消息。日志记录 route_selected；消息只在节点成功提案后标记消费，不回放已消费消息。

## 最终状态与选择

workflow_status 描述调度是否结束，task_status 描述测试业务结论。完成调度可以是 partial、needs_review 或 failed，只有 verified latest execution + coverage_target_met 才是 completed。流程结束后不能再次 run；inspect 仅重新读取状态。

每次提交候选观察后复用 select_best_candidate()：全通过优先，其次目标覆盖、覆盖是否已知、分支/行覆盖、通过数；完全同分保留较早候选。Workflow 和 AgentState 的 selected_round / best_available_round 始终同步；历史按轮次追加，只有当前未执行候选可补充一次执行观察。

summary.json 保存 selected_round、selection_reason、selected_all_passed 和 latest_all_passed。后轮失败或被门禁拒绝时，可以导出先前可用测试，但 task_status 不会因历史通过而变 completed。最终测试和 subject.py 按原字节复制到 final/，所有 rounds/ 中的测试、计划、LLM 响应和执行证据保留。没有可用候选则不创建 final/。

外部请求计数与客户端调用分开。结构化模型错误报告已知尝试数时也计入 api_calls；无法取得次数时 summary.external_requests=null，并另存 known_external_requests，不能把未知量写成 0。离线请求数为 0。执行模式从 assessment 记录，注入执行器明确标 injected。

report.md 和 summary.json 完成后才记录 workflow_finished；导出失败会记录 export_error 并向调用方抛出，不能宣称报告已写入。状态原子替换失败时保留最后成功快照，不承诺续跑或跨文件事务。

## CLI 与复现

必需输入为 --source、--spec；可选 --output、--max-rounds、--timeout、--demo、可重复 --fixture 和真实模式 --env-file。离线模式拒绝 --env-file，真实模式拒绝 --fixture；离线内置 calculation、validation、discount 候选，其他输入须指定 JSON。

退出码 0 为 completed，1 为其他业务结论或节点错误，2 为输入/配置/产物持久化错误。`scripts/p4_baseline.py` 通过正式 CLI 运行确定案例再执行回归；--skip-tests 只复现案例，--tests-only 在已有 CLI 索引上更新回归证据。阶段结果见 [../evaluation/p4.md](../evaluation/p4.md)。

P5 的 `scripts/p5_acceptance.py` 进一步保存统一系统矩阵和真实模型索引：--offline --tests 执行本地案例与回归，--real 明确启用三个必需真实模型案例，--collect-real 读取指定既有任务，--refresh 重新校验索引里的文件与交接。离线与读取模式不读取密钥、不发出模型请求；每个新案例使用独立目录，历史记录不覆盖。结果见 [../evaluation/acceptance.md](../evaluation/acceptance.md)。

流程定义可以追加无消息依赖的尾部节点：实现同步接口、注册稳定标识并追加 NodeDefinition 即可。P6 已交付 ResultSummaryNode 和 --with-summary CLI，记录导出前业务结论并返回摘要产物，不修改主循环的业务路由。实现步骤见 [extensions.md](extensions.md)。

P6 显式记录 node_output_saved、message_consumed 和 workflow_stopped。消息事件关联候选与 assessment 路径，消费事件轮次是接收轮次；无消息任务也保留空 messages.jsonl。报告包含逐轮计数、实测覆盖可用性、节点职责/调用、消息、路由和原始文件链接；inspect --summary 校验证据并只读重建摘要，不改写旧记录。完整字段和持久化边界见 [reporting.md](reporting.md)。
