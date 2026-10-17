"""Evidence-backed summaries and Markdown reports; inspection never writes."""

import json
import shutil
from urllib.parse import quote

from .contracts import AgentContext, AgentResult, ExecutionAssessment
from .events import Event
from .report_metrics import model_usage
from .quality_reporting import quality_details
from .state import WorkflowState
from .storage import atomic_write, file_digest, task_path, verify_artifact

ROLES = {
    "test_generation": "根据源码与规格规划、生成或修订测试；不执行 pytest。",
    "test_execution": "校验候选、执行 pytest/coverage、保守归因并提出路由建议；不生成测试。",
    "test_quality_review": "依据规格审查测试场景并提出带证据的质量反馈；不执行测试或改写候选。",
    "result_summary": "读取已提交结论并保存摘要；无模型调用，不改变业务结论。",
}


def build_summary(state: WorkflowState, final=None, *, strict_quality=False) -> dict:
    """Read only recorded observations, including unsuccessful node invocations."""
    nodes = []
    for path in sorted((state.run_dir / "node_results").glob("*/*/context.json")):
        context = AgentContext.model_validate_json(path.read_text(encoding="utf-8"))
        if context.run_id != state.run_id or context.run_dir != state.run_dir:
            raise ValueError("recorded node context belongs to another task")
        result_path = path.parent / "result.json"
        result = AgentResult.model_validate_json(result_path.read_text(encoding="utf-8")) if result_path.exists() else None
        nodes.append({
            "node": context.agent_id, "round": context.round,
            "context": path.relative_to(state.run_dir).as_posix(),
            "inbox": [message.message_id for message in context.inbox],
            "result": result_path.relative_to(state.run_dir).as_posix() if result else None,
            "output_status": result.status if result else "no_result",
            "protocol_matches": bool(result and (result.run_id, result.round, result.agent_id) ==
                                     (context.run_id, context.round, context.agent_id)),
            "error_code": result.error_code if result else None,
            "generation": result.generation.model_dump(mode="json") if result and result.generation else None,
            "review": result.review.model_dump(mode="json") if result and result.review else None,
            "quality_assessment": result.quality_assessment.model_dump(mode="json") if result and result.quality_assessment else None,
        })
    events = tuple(Event.model_validate_json(line) for line in (state.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines())
    if any(event.run_id != state.run_id or event.seq != number for number, event in enumerate(events, 1)):
        raise ValueError("event journal has a foreign task or nonconsecutive sequence")
    # A journal can lead the last atomic state after a crash; never invent a commit.
    committed = tuple(event for event in events if event.seq <= state.last_event_seq)
    for node in nodes:
        start = next((event for event in committed if event.round == node["round"] and event.node == node["node"]
                      and event.type == "node_started"), None)
        end = next((event for event in reversed(committed) if event.round == node["round"] and event.node == node["node"]
                    and event.type in {"node_finished", "node_failed"}), None)
        node.update(committed=bool(end and end.type == "node_finished"),
                    start_seq=start.seq if start else None, duration_seconds=end.duration_seconds if end else None)
    nodes.sort(key=lambda node: node["start_seq"] if node["start_seq"] is not None else state.last_event_seq + 1)
    usage = model_usage(nodes, measured=state.config.mode != "demo")
    selected = state.agent_state.history[state.selected_round - 1] if state.selected_round else None
    latest = state.agent_state.history[-1] if state.agent_state.history else None
    rounds = []
    for candidate in state.agent_state.history:
        references = tuple(item for item in state.artifacts if item.round == candidate.number and item.kind != "subject")
        assessment_ref = next((item for item in references if item.kind == "assessment"), None)
        assessment = ExecutionAssessment.model_validate_json(verify_artifact(state.run_dir, assessment_ref).read_text(
            encoding="utf-8")) if assessment_ref else None
        rounds.append({
            "round": candidate.number, "candidate_id": f"{state.run_id}:{candidate.number}",
            "validation_error": candidate.validation_error,
            "questions": list(candidate.generated.questions) if candidate.generated else [],
            "execution": candidate.execution.model_dump(mode="json") if candidate.execution else None,
            "coverage": candidate.coverage.model_dump(mode="json") if candidate.coverage else None,
            "assessment": assessment.model_dump(mode="json") if assessment else None,
            "evidence": [item.model_dump(mode="json") for item in references],
        })
    agents = {}
    for identity in dict.fromkeys(node["node"] for node in nodes):
        calls = [node for node in nodes if node["node"] == identity]
        agents[identity] = {"role": ROLES.get(identity, "已注册的扩展节点。"), "invocations": len(calls),
                            "committed_results": sum(node["committed"] for node in calls)}
    messages = [{"message_id": item.message_id, "type": item.type, "sender": item.sender, "receiver": item.receiver,
                 "source_round": item.round, "candidate_id": item.payload.candidate_id, "reason": item.payload.reason,
                 "artifact": item.payload.artifact.path, "consumed": item.message_id in state.consumed_message_ids,
                 "consumer_round": next((node["round"] for node in nodes if node["committed"] and item.message_id in node["inbox"]), None)
                                   if item.message_id in state.consumed_message_ids else None,
                 "consumer_node": next((node["node"] for node in nodes if node["committed"] and item.message_id in node["inbox"]), None)
                                  if item.message_id in state.consumed_message_ids else None,
                 "findings": [finding.model_dump(mode="json") for finding in item.payload.quality_feedback.findings] if item.payload.quality_feedback else []}
                for item in state.messages]
    quality = quality_details(state, rounds, nodes, messages, strict=strict_quality)
    return {
        "schema_version": "1.2", "run_id": state.run_id, "mode": state.config.mode,
        "generation_mode": "offline" if state.config.mode == "offline" else "real" if state.config.mode == "real" else "demo",
        "workflow_status": state.workflow_status, "task_status": state.task_status,
        "stop_reason": state.stop_reason, "error_code": state.error_code,
        "started_at": state.started_at.isoformat(), "finished_at": state.finished_at.isoformat() if state.finished_at else None,
        "duration_seconds": (state.finished_at - state.started_at).total_seconds() if state.finished_at else None,
        "rounds": len(rounds), "round_details": rounds, "selected_round": state.selected_round,
        "selection_reason": ("选择当前已通过质量审查的候选；执行与批准证据对应同一轮。"
                             if state.config.quality_review_enabled and state.quality_status == "passed"
                             else "全通过优先，再按目标覆盖、覆盖是否可用、分支/行覆盖和通过数排序；同分保留早轮。") if selected else "没有已执行且可用的候选。",
        "selected_all_passed": selected.execution.all_passed if selected else None,
        "latest_all_passed": latest.execution.all_passed if latest and latest.execution else None,
        "selected_execution": selected.execution.model_dump(mode="json") if selected else None,
        "selected_coverage": selected.coverage.model_dump(mode="json") if selected and selected.coverage else None,
        "final_test_file": str(final) if final else None, "final_test_sha256": file_digest(final) if final else None,
        "generation_calls": usage["generation"]["client_calls"], "review_calls": usage["review"]["client_calls"],
        "external_requests": usage["total"]["external_requests"],
        "total_external_requests": usage["total"]["external_requests"],
        "known_external_requests": usage["total"]["known_external_requests"],
        "token_usage": usage["total"]["token_usage"], "model_usage": usage,
        "generation_external_requests": usage["generation"]["external_requests"],
        "review_external_requests": usage["review"]["external_requests"],
        **quality, "exported_quality_approved": quality["selected_quality_approved"] if final else None,
        "execution_observation_modes": sorted({row["assessment"]["observation_mode"] for row in rounds if row["assessment"]}),
        "execution_tool_calls": sum(row["assessment"]["tool_calls"] for row in rounds if row["assessment"]),
        "pytest_processes": sum(row["assessment"]["pytest_started"] for row in rounds if row["assessment"]),
        "coverage_target": {"line_percent": 90, "branch_percent": 80, "no_branches": "not_applicable"},
        "agents": agents, "node_invocations": nodes,
        "messages": messages,
        "routes": [event.model_dump(mode="json") for event in committed if event.type in {"route_selected", "workflow_stopped", "workflow_error"}],
        "last_event_seq": state.last_event_seq, "journal_last_seq": len(events),
        "report_file": str(state.run_dir / "report.md"), "state_file": str(state.run_dir / "state.json"),
    }


def inspect_summary(state: WorkflowState) -> dict:
    """Validate registered evidence and final copies; no node, client or writes."""
    state = WorkflowState.model_validate_json(state.canonical_json())
    for reference in state.artifacts:
        verify_artifact(state.run_dir, reference)
    final = None
    if state.selected_round:
        path = state.run_dir / "final/test_subject.py"
        if path.exists() or state.workflow_status == "finished" and state.config.mode != "demo":
            final = task_path(state.run_dir, "final/test_subject.py")
            selected = next(item for item in state.artifacts if item.kind == "tests" and item.round == state.selected_round)
            subject = next(item for item in state.artifacts if item.kind == "subject")
            if file_digest(final) != selected.sha256 or file_digest(task_path(state.run_dir, "final/subject.py")) != subject.sha256:
                raise ValueError("final copies differ from selected evidence")
    return build_summary(state, final, strict_quality=True)


def cell(value):
    if value is None:
        return "未知/不适用"
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def link(path, label=None):
    return f"[{cell(label or path)}]({quote(path, safe='/')})"


def render_report(summary: dict) -> str:
    rows = ["# Workflow 测试报告", "", f"任务：`{summary['run_id']}`。",
            f"编排状态：`{summary['workflow_status']}`；测试结论：`{summary['task_status']}`；停止原因：`{summary['stop_reason']}`；错误码：`{summary['error_code']}`。", "",
            f"生成模式：`{summary['generation_mode']}`；执行观察：`{', '.join(summary['execution_observation_modes']) or '未执行'}`。",
            f"生成客户端调用 {cell(summary['generation_calls'])} 次，审查客户端调用 {cell(summary['review_calls'])} 次，外部模型请求 {cell(summary['external_requests'])} 次（已知 {summary['known_external_requests']} 次）；"
            f"执行工具 {summary['execution_tool_calls']} 次，pytest 启动 {summary['pytest_processes']} 次。",
            f"开始：{summary['started_at']}；结束：{cell(summary['finished_at'])}；总耗时/秒：{cell(summary['duration_seconds'])}。", "",
            "## 协作与节点调用", "", "| 节点 | 职责 | 调用/提交 |", "|---|---|---|"]
    for name, agent in summary["agents"].items():
        rows.append(f"| {name} | {agent['role']} | {agent['invocations']}/{agent['committed_results']} |")
    rows += ["", "## 模型请求与用量", "", "| 分项 | 客户端调用 | 请求尝试 | 外部请求 / 已知 | Prompt / Completion / Total tokens | 已知 token 小计 |",
             "|---|---|---|---|---|---|"]
    for name, values in summary["model_usage"].items():
        tokens = values["token_usage"]
        rows.append(f"| {name} | {cell(values['client_calls'])} | {cell(values['request_attempts'])} | {cell(values['external_requests'])} / {values['known_external_requests']} | "
                    f"{cell(tokens['prompt_tokens'])} / {cell(tokens['completion_tokens'])} / {cell(tokens['total_tokens'])} | {cell(values['known_token_usage'])} |")
    rows += ["", "未知请求或用量保留未知，已知小计不等于完整总量；客户端内部格式修复不增加候选轮次。"]
    rows += ["", "| 轮次 | 节点 | 输出/提交 | 耗时/秒 | 输入 | 输出 |", "|---|---|---|---|---|---|"]
    for node in summary["node_invocations"]:
        rows.append(f"| {node['round']} | {node['node']} | {node['output_status']}/{node['committed']} | {cell(node['duration_seconds'])} | "
                    f"{link(node['context'], 'Context')} | {link(node['result'], 'Result') if node['result'] else '无结果，见错误事件'} |")
    rows += ["", "## 最终选择", "", f"选中轮次：`{summary['selected_round']}`。{summary['selection_reason']}",
             f"选中候选全部通过：`{summary['selected_all_passed']}`；最新候选全部通过：`{summary['latest_all_passed']}`。",
             f"选中候选质量批准：`{summary['selected_quality_approved']}`；最新候选质量批准：`{summary['latest_quality_approved']}`；实际导出已批准：`{summary['exported_quality_approved']}`。",
             "选中较早候选不代表最新候选通过；needs_review 的失败断言继续保留，覆盖率不能证明行为正确。", "",
             "目标：行覆盖 ≥90%；存在分支时分支覆盖 ≥80%。无分支为不适用；未知计数不记为 0；覆盖不可用不记为达标。"]
    if summary["final_test_file"]:
        rows += ["", f"最终文件：{link('final/test_subject.py', '测试')}、{link('final/subject.py', '源码副本')}；SHA-256：`{summary['final_test_sha256']}`。"]
    else:
        rows += ["", "没有可导出的已执行可用测试。"]
    rows += ["", "## 各轮实测与决策", "", "| 轮次 | 校验 | 执行 | 收集/执行 | 通过/失败/错误 | 跳过/xfail/xpass | 行/分支 % | 建议 |",
             "|---|---|---|---|---|---|---|---|"]
    for item in summary["round_details"]:
        execution, coverage, assessment = item["execution"] or {}, item["coverage"] or {}, item["assessment"] or {}
        rows.append(f"| {item['round']} | {'rejected' if item['validation_error'] else 'accepted'} | {execution.get('kind', '未执行')} | "
                    f"{cell(execution.get('collected'))}/{cell(execution.get('executed'))} | "
                    f"{cell(execution.get('passed'))}/{cell(execution.get('failed'))}/{cell(execution.get('errors'))} | "
                    f"{cell(execution.get('skipped'))}/{cell(execution.get('xfailed'))}/{cell(execution.get('xpassed'))} | "
                    f"{cell(coverage.get('line_percent'))}/{cell(coverage.get('branch_percent'))} | {assessment.get('recommendation', '无评估')} |")
    for item in summary["round_details"]:
        assessment, coverage = item["assessment"] or {}, item["coverage"] or {}
        rows += ["", f"### 第 {item['round']} 轮：`{item['candidate_id']}`", "",
                 f"校验错误：{cell(item['validation_error'])}；规格问题：{cell(item['questions'])}。",
                 f"建议理由：{cell(assessment.get('reason'))}；停止建议：{cell(assessment.get('stop_reason'))}。",
                 f"覆盖状态：{cell(coverage.get('status'))}；不可用原因：{cell(coverage.get('error'))}；"
                 f"缺失行：{cell(coverage.get('missing_lines'))}；缺失分支：{cell(coverage.get('missing_branches'))}。", ""]
        feedback = assessment.get("feedback", {})
        for issue in feedback.get("issues", []):
            rows.append(f"- 问题 `{issue['kind']}`：{cell(issue['summary'])}；规格/场景证据：{cell(issue.get('evidence'))}。")
        if feedback.get("protected_tests"):
            rows.append(f"- 受保护的已通过测试：{cell(feedback['protected_tests'])}。")
        rows.extend(f"- {link(ref['path'], ref['kind'])}（SHA-256 `{ref['sha256']}`）" for ref in item["evidence"])
    rows += ["", "## 质量审查与规格依据", "", f"启用：`{summary['quality_review_enabled']}`；质量状态：`{summary['quality_status']}`；停止原因：`{summary['quality_stop_reason']}`。",
             f"当前未解决发现来源轮次：{cell(summary['unresolved_findings_round'])}；数量：{len(summary['unresolved_findings'])}。",
             "审查意见不等于源码缺陷实测证明；基础执行达标与质量批准分别记录。"]
    for item in summary["round_details"]:
        quality = item["quality"]
        rows += ["", f"### 第 {item['round']} 轮质量：`{quality['status']}`", "", f"原因：{cell(quality['reason'])}；证据已核验：{cell(quality['evidence_verified'])}。"]
        assessment = quality["assessment"]
        if assessment:
            rows += [f"审查者：test_quality_review；基础执行结论：`{(item['assessment'] or {}).get('task_status')}`；质量决定：`{assessment['decision']['verdict']}`。",
                     f"记录：{link(quality['assessment_path'])}；响应/遥测：{link(quality['response_path'])}；基础执行：{link(assessment['execution_assessment']['path'])}。"]
            for finding in assessment["decision"]["findings"]:
                rows.append(f"- `{cell(finding['finding_id'])}` / {cell(finding['category'])} / 阻塞 {finding['blocking']}：{cell(finding['description'])}；函数 {cell(finding['target_functions'])}；测试 {cell(finding['related_tests'])}。")
                rows.append(f"  规格原文：{cell(finding['spec_quote'])}；测试原文：{cell(finding['test_code_quote'])}；建议场景：{cell(finding['suggested_case'])}。")
            rows.extend(f"- 规格问题：{cell(question)}" for question in assessment["decision"]["questions"])
        for feedback in quality["feedback"]:
            rows.append(f"- 质量反馈 `{feedback['message_id']}`：来源第 {feedback['source_round']} 轮，已消费 {feedback['consumed']}，消费轮次 {cell(feedback['consumer_round'])}，节点 {cell(feedback['consumer_node'])}。")
    rows += ["", "## 消息与路由", "", "| 消息 | 来源轮次/候选 | 发送 → 接收 | 已消费 / 消费轮次 | 证据 |", "|---|---|---|---|---|"]
    for message in summary["messages"]:
        rows.append(f"| {message['message_id']} | {message['source_round']}/{message['candidate_id']} | "
                    f"{message['sender']} → {message['receiver']} | {message['consumed']} / {cell(message['consumer_round'])} | {link(message['artifact'])} |")
    rows += ["", "| 事件序号 | 轮次 | 事件 | 动作 | 停止原因 |", "|---|---|---|---|---|"]
    for route in summary["routes"]:
        rows.append(f"| {route['seq']} | {cell(route['round'])} | {route['type']} | {cell(route['action'])} | {cell(route['stop_reason'])} |")
    rows += ["", "## 状态与原始证据", "", "- [状态快照](state.json)、[机器摘要](summary.json)。",
             "- [递增事件](events.jsonl)、[消息日志](messages.jsonl)。",
             "- 本报告读取导出前已提交的证据；workflow_finished 在报告成功保存后记录，最终事件以日志与 state.json 为准。",
             "- 日志与状态不构成跨文件事务；持久化失败保留最后成功快照，历史只读查看，不支持断点续跑。"]
    return "\n".join(rows) + "\n"


def export_result(state: WorkflowState) -> None:
    selected = state.agent_state.history[state.selected_round - 1] if state.selected_round else None
    final = None
    if selected:
        reference = next(item for item in state.artifacts if item.kind == "tests" and item.round == selected.number)
        source = verify_artifact(state.run_dir, reference)
        final = state.run_dir / "final/test_subject.py"
        final.parent.mkdir(exist_ok=False)
        shutil.copyfile(source, final)
        subject = next(item for item in state.artifacts if item.kind == "subject")
        shutil.copyfile(verify_artifact(state.run_dir, subject), final.parent / "subject.py")
    summary = build_summary(state, final)
    atomic_write(state.run_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    atomic_write(state.run_dir / "report.md", render_report(summary))
