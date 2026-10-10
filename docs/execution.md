# P3 单候选执行与评估

版本：P3 / 2026-10-10。`TestExecutionAgent.run(context) -> AgentResult` 只处理当前候选，返回真实执行观察和动作建议。节点不调用生成客户端，也不调用其他节点。独立验收入口为 `python scripts/p3_baseline.py`，完整产物索引见 `evaluation/p3_results.json`。

## 执行门禁

输入必须是 running 状态中的 test_execution 节点，来源轮次等于当前历史末轮。执行前核对任务内工作区、原始输入和 subject.py 字节、候选交接 JSON、测试内容及所有生成产物摘要。可执行候选必须持有尚未消费、已登记的同轮 candidate_ready 消息；测试路径只能是内层 rounds/NN/test_subject.py。路径解析拒绝越界文件，任务根和工作区不得为符号链接或 junction。

历史已有执行观察、当前轮已有 evaluations/NN 或 execution 运行目录时拒绝再次启动。即使使用旧 Context 重试，也不能覆盖同轮证据；异常后保留诊断目录，需要新建任务重跑。

未通过生成校验的候选没有 candidate_ready。P4 Workflow 会将该候选交给评估节点；节点仅生成 assessment 和修复/复核/停止建议，tool_calls=0、pytest_started=false、execution/coverage=null。宿主在执行前再次调用 validate_generated_tests，校验失败同样不会启动 pytest。

## 一次实测与证据

每个通过门禁的候选仅调用一次 `run_tests_with_coverage(workspace, timeout=配置秒数, round_number=当前轮次)`。pytest 在 coverage 下运行一次，随后从同次数据库导出数据，导出过程不再次执行测试。计数由插件 checkpoint 和 JUnit 解析，不能从 stdout 中搜索“passed”来推断结果。

原工具文件完整保留在 `agent_workspace/rounds/NN/executions/run-*/`，包括 execution.json、pytest_result.json、pytest.xml、stdout/stderr、coverage 导出诊断和结果、coverage_source_hints.json。超时或启动失败时部分文件可能尚未生成，节点只登记实际存在的证据。coverage.json 只包含目标 subject.py；阈值沿用行 90%、分支 80%，无分支时 branch_percent=null，按无分支目标处理。

外层 `evaluations/NN/assessment.json` 保存单独的 ExecutionAssessment。所有已登记文件带任务相对路径、候选标识、轮次和 SHA-256。节点在执行后再核对源码和测试文件；发生变化保留实测观察，但建议 stop_error，禁止完成结论。写入或解析异常返回安全 error_code 和已经存在的任务内 diagnostic_paths，不能夹带成功状态提交。

## 评估与反馈

节点复用 `classify_failures()` 和 `choose_action()`。Assessment 的 task_status/stop_reason 是建议，P3 不提交最终业务结论或选中历史候选。状态持久化由协调者通过 apply_result 校验后完成。

| 原 AgentAction / 停止原因 | P3 recommendation | 行为 |
|---|---|---|
| finish / coverage_target_met | finish | 实测全通过、目标覆盖达到；提供 completed 建议 |
| repair_generated_test | repair_tests | 测试名称、导入或候选校验错误；反馈修复依据 |
| add_missing_cases | extend_tests | 通过但覆盖不足；反馈缺失行、分支、函数和调用链 |
| request_review | request_review | 保留失败断言、规格依据和不确定性 |
| finish / execution_timeout、unrecoverable_error | stop_error | 超时、覆盖不可用或完整性错误；保留真实诊断 |
| finish / round_limit_reached、no_progress | finish | 带停止原因；不等同完成 |

ExecutionFeedback 使用原 ExecutionResult/CoverageResult，增加 validation_error、issues、protected_tests。source_hints_json 保存 JSON 对象的文本，每次解码得到独立结构，避免共享 vendor 嵌套字典；其中包含 coverage_source_hints、affected_functions、caller_chain_functions、direct_call_evidence、prior_strategy 等原策略信息。修复和补充建议发出 execution_feedback，消息负载内含完整反馈，并引用同轮 assessment 文件。

EVALUATION_WORKFLOW 的 feedback_routes 声明合法发送方向 test_execution → test_generation。apply_result 校验消息、落盘 assessment、候选观察、动作和证据一致。P3 验收脚本手动传递一次低覆盖反馈；P4 TESTING_WORKFLOW 通过同一边自动反馈，并校验预算、路由和最终业务结论，详见 [workflow.md](workflow.md)。

## 已验证的边界

多参数或复杂断言失败只给 ambiguous_specification，不能直接判源码缺陷。discount 的 100→90 单参数等值断言有规格表格支持，给 suspected_source_defect，原源码和断言均保留。可定位到测试文件的 NameError/ImportError 建议修复；参数化收集错误缺少明确归因时请求复核。

空测试候选在生产门禁被拒绝，零 pytest。另有手工构造的空测试工作区直接调用底层工具，取得真实 no_tests/collected=0，再以 observation_mode=injected 验证修复建议；这是底层诊断验收，不表示无效候选可以穿过门禁。

受控无限循环用 2 秒超时验证 pytest 子进程已经停止。coverage 导出失败采用本地 OSError 注入；pytest 仍真实执行且通过 3 项，coverage.status=unavailable、指标为 null，建议 stop_error。可注入 executor 的观察标为 injected；默认执行器标为 real，表示真实测试执行，生成客户端是否离线独立记录。
