"""Deterministic user-facing result summaries and Markdown reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import AgentState, CandidateRound, StopReason, TaskStatus

if TYPE_CHECKING:
    from .agent import ActionDecision
    from .tracing import TraceWriter


STATUS_LABELS = {
    TaskStatus.COMPLETED: "完成",
    TaskStatus.PARTIAL: "部分完成",
    TaskStatus.NEEDS_REVIEW: "需要人工复核",
    TaskStatus.FAILED: "失败",
}

STOP_LABELS = {
    StopReason.COVERAGE_TARGET_MET: "测试通过且覆盖目标已达到",
    StopReason.ROUND_LIMIT_REACHED: "已达到最大候选轮数",
    StopReason.NO_PROGRESS: "继续迭代未带来进展",
    StopReason.SUSPECTED_SOURCE_DEFECT: "测试发现疑似源码缺陷",
    StopReason.AMBIGUOUS_SPECIFICATION: "行为规格存在歧义",
    StopReason.EXECUTION_TIMEOUT: "测试执行超时",
    StopReason.UNRECOVERABLE_ERROR: "发生不可恢复错误",
}


def _selected(state: AgentState) -> CandidateRound | None:
    if state.best_available_round is None:
        return None
    return state.history[state.best_available_round - 1]


def _percent(value: float | None) -> str:
    return "不适用" if value is None else f"{value:.1f}%"


def _number(value: int | float | None) -> str:
    return "未知" if value is None else str(value)


def _cell(value: object, *, limit: int = 180) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _inputs(scenario: Any) -> str:
    value = {"args": list(scenario.inputs.args), "kwargs": scenario.inputs.kwargs}
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _metrics(state: AgentState, selected: CandidateRound | None) -> dict[str, Any]:
    execution = selected.execution if selected else None
    coverage = selected.coverage if selected else None
    collected = execution.collected if execution else None
    passed = execution.passed if execution else None
    pass_percent = (100 * passed / collected) if passed is not None and collected else None
    return {
        "candidate_rounds": state.current_round,
        "executed_rounds": sum(item.execution is not None for item in state.history),
        "selected_round": state.best_available_round,
        "execution_kind": execution.kind if execution else None,
        "collected": collected,
        "executed": execution.executed if execution else None,
        "passed": passed,
        "failed": execution.failed if execution else None,
        "errors": execution.errors if execution else None,
        "skipped": execution.skipped if execution else None,
        "pass_percent": pass_percent,
        "duration_seconds": execution.duration_seconds if execution else None,
        "coverage_status": coverage.status if coverage else None,
        "covered_lines": coverage.covered_lines if coverage else None,
        "total_lines": coverage.total_lines if coverage else None,
        "line_percent": coverage.line_percent if coverage else None,
        "covered_branches": coverage.covered_branches if coverage else None,
        "total_branches": coverage.total_branches if coverage else None,
        "branch_percent": coverage.branch_percent if coverage else None,
        "coverage_target_met": coverage.meets_target if coverage else False,
        "api_calls": state.api_calls,
        "token_usage": state.token_usage.model_dump() if state.token_usage else None,
    }


def _conclusion(state: AgentState, decision: ActionDecision, metrics: dict[str, Any]) -> dict[str, str]:
    stop = state.stop_reason
    if stop == StopReason.COVERAGE_TARGET_MET:
        detail = (
            f"选中第 {state.best_available_round} 轮测试；共收集 {metrics['collected']} 个测试，"
            f"{metrics['passed']} 个通过、{metrics['failed']} 个失败、{metrics['errors']} 个错误。"
            f"行覆盖率为 {_percent(metrics['line_percent'])}，分支覆盖率为 "
            f"{_percent(metrics['branch_percent'])}。在本报告列出的规格场景内，未观察到失败。"
        )
        return {"level": "passed", "title": "通过：测试全部成功并达到覆盖目标", "detail": detail}
    if stop == StopReason.SUSPECTED_SOURCE_DEFECT:
        return {
            "level": "review",
            "title": "需要复核：测试发现疑似源码缺陷",
            "detail": f"实际执行出现 {metrics['failed'] or 0} 个失败断言；失败预期有规格依据。{decision.reason}",
        }
    if stop == StopReason.AMBIGUOUS_SPECIFICATION:
        return {"level": "review", "title": "需要复核：规格不足以确定唯一预期", "detail": decision.reason}
    if decision.status == TaskStatus.PARTIAL:
        return {
            "level": "partial",
            "title": "部分完成：已保留当前最佳测试，但未满足完整完成条件",
            "detail": decision.reason,
        }
    return {
        "level": "failed",
        "title": "未完成：没有得到可确认的完整测试结果",
        "detail": decision.reason,
    }


def build_summary(
    state: AgentState, decision: ActionDecision, final_path: Path | None, trace: TraceWriter,
) -> dict[str, Any]:
    """Build a stable machine-readable summary with the actual result up front."""
    selected = _selected(state)
    metrics = _metrics(state, selected)
    generated = selected.generated if selected else None
    scenarios = generated.plan.scenarios if generated else ()
    return {
        "run_id": state.run_id,
        "status": decision.status,
        "stop_reason": state.stop_reason,
        "reason": decision.reason,
        "conclusion": _conclusion(state, decision, metrics),
        "metrics": metrics,
        "scope": {
            "target_functions": list(generated.plan.target_functions) if generated else [],
            "entry_functions": list(generated.plan.entry_functions) if generated else [],
            "relationships": [item.model_dump(mode="json") for item in generated.plan.relationships] if generated else [],
            "strategy": [item.model_dump(mode="json") for item in generated.plan.strategy] if generated else [],
            "planned_scenarios": len(scenarios),
            "unit_scenarios": sum(item.test_kind == "unit" for item in scenarios),
            "collaboration_scenarios": sum(item.test_kind == "collaboration" for item in scenarios),
            "categories": sorted({item.category for item in scenarios}),
            "mapped_test_functions": [item.test_function for item in generated.mappings] if generated else [],
        },
        "selected_round": state.best_available_round,
        "final_test_file": str(final_path) if final_path else None,
        "candidate_rounds": state.current_round,
        "executed_rounds": metrics["executed_rounds"],
        "api_calls": state.api_calls,
        "token_usage": state.token_usage.model_dump() if state.token_usage else None,
        "trace_file": str(trace.path),
        "metadata_file": str(trace.metadata_path),
        "unresolved_issues": [item.model_dump(mode="json") for item in state.issues],
    }


def render_markdown_report(state: AgentState, decision: ActionDecision, summary: dict[str, Any]) -> str:
    """Render a human-readable report for a person reviewing one run."""
    selected = _selected(state)
    generated = selected.generated if selected else None
    execution = selected.execution if selected else None
    coverage = selected.coverage if selected else None
    metrics = summary["metrics"]
    conclusion = summary["conclusion"]
    source = state.source_info

    lines = [
        "# 自动测试生成报告",
        "",
        f"> **最终结论：{conclusion['title']}**  ",
        f"> {conclusion['detail']}",
        "",
        "## 结果概览",
        "",
        "| 项目 | 结果 |",
        "|---|---|",
        f"| 任务状态 | {STATUS_LABELS.get(decision.status, decision.status)} |",
        f"| 停止原因 | {STOP_LABELS.get(state.stop_reason, state.stop_reason)} |",
        f"| 被测源码 | `{_cell(state.request.source_path)}` |",
        f"| 行为规格 | `{_cell(state.request.spec_path)}` |" if state.request.spec_path else "| 行为规格 | 未提供文件 |",
        f"| 判定依据 | {'行为规格' if state.oracle_mode == 'specification' else '当前实现特征'} |",
        f"| 候选轮数 / 实际执行轮数 | {state.current_round} / {metrics['executed_rounds']} |",
        f"| 最终选中轮次 | {_number(state.best_available_round)} |",
        "",
        "## 核心数据",
        "",
        "### 测试执行",
        "",
        "| 收集 | 执行 | 通过 | 失败 | 错误 | 跳过 | 通过率 | 用时 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| {_number(metrics['collected'])} | {_number(metrics['executed'])} | {_number(metrics['passed'])} | "
        f"{_number(metrics['failed'])} | {_number(metrics['errors'])} | {_number(metrics['skipped'])} | "
        f"{_percent(metrics['pass_percent'])} | "
        f"{_number(round(metrics['duration_seconds'], 3) if metrics['duration_seconds'] is not None else None)} 秒 |",
        "",
        "### 覆盖率",
        "",
        "| 指标 | 已覆盖 | 总计 | 覆盖率 | 缺失 |",
        "|---|---:|---:|---:|---|",
    ]
    if coverage and coverage.status == "available":
        lines.extend([
            f"| 代码行 | {coverage.covered_lines} | {coverage.total_lines} | {_percent(coverage.line_percent)} | "
            f"{_cell(', '.join(map(str, coverage.missing_lines)) or '无')} |",
            f"| 分支 | {coverage.covered_branches} | {coverage.total_branches} | {_percent(coverage.branch_percent)} | "
            f"{_cell(', '.join(f'{arc.start}→{arc.end}' for arc in coverage.missing_branches) or '无')} |",
            "",
            f"覆盖目标（行覆盖率至少 90%，存在分支时分支覆盖率至少 80%）："
            f"**{'已达到' if coverage.meets_target else '未达到'}**。",
        ])
    else:
        reason = coverage.error if coverage else "没有可用的测试执行结果"
        lines.extend([f"| 覆盖率 | 未取得 | 未取得 | 未取得 | {_cell(reason)} |", ""])

    lines.extend(["", "## 测试范围与场景", ""])
    if generated:
        plan = generated.plan
        lines.extend([
            f"目标函数：{', '.join(f'`{name}`' for name in plan.target_functions)}",
            "",
            "| 场景 | 类别 | 类型 | 目标函数 | 输入 | 预期行为 | 依据 |",
            "|---|---|---|---|---|---|---|",
        ])
        for scenario in plan.scenarios:
            lines.append(
                f"| {_cell(scenario.scenario_id)} | {_cell(scenario.category)} | {_cell(scenario.test_kind)} | "
                f"`{_cell(scenario.target_function)}` | `{_cell(_inputs(scenario))}` | "
                f"{_cell(scenario.expected_behavior)} | {_cell(scenario.expected_basis.kind)}："
                f"{_cell(scenario.expected_basis.detail)} |"
            )
        lines.extend(["", "测试函数与场景映射：", ""])
        for mapping in generated.mappings:
            lines.append(f"- `{mapping.test_function}`：{', '.join(f'`{sid}`' for sid in mapping.scenario_ids)}")
        if generated.assumptions:
            lines.extend(["", "生成测试采用的假设：", ""])
            lines.extend(f"- {_cell(item, limit=500)}" for item in generated.assumptions)
        if generated.questions:
            lines.extend(["", "待确认问题：", ""])
            lines.extend(f"- {_cell(item, limit=500)}" for item in generated.questions)

        if plan.schema_version == "1.1":
            lines.extend(["", "## 函数关联与测试策略", ""])
            lines.append(
                "候选入口函数："
                + ("、".join(f"`{_cell(name)}`" for name in plan.entry_functions) if plan.entry_functions else "无")
            )
            lines.extend(["", "| 关联 ID | 类型 | 涉及函数 | 置信度 | 依据 | 待确认 |",
                          "|---|---|---|---|---|---|"])
            if plan.relationships:
                for relation in plan.relationships:
                    evidence = "；".join(
                        f"{item.kind}：{item.detail}"
                        + (f"（第 {item.line} 行）" if item.line else "")
                        + (f" [调用点：{', '.join(item.call_site_ids)}]" if item.call_site_ids else "")
                        for item in relation.evidence
                    )
                    lines.append(
                        f"| {_cell(relation.relation_id)} | {_cell(relation.kind)} | "
                        f"{_cell(', '.join(relation.involved_functions))} | {_cell(relation.confidence)} | "
                        f"{_cell(evidence, limit=500)} | "
                        f"{_cell('；'.join(relation.unresolved_questions) or '无', limit=300)} |"
                    )
            else:
                lines.append("| 无 | — | — | — | 未发现可纳入计划的函数关联 | — |")
            lines.extend(["", "| 对象 | 决策 | 理由 |", "|---|---|---|"])
            for choice in plan.strategy:
                subject = choice.function_name if choice.subject_kind == "function" else choice.relationship_id
                lines.append(
                    f"| {_cell(choice.subject_kind)}：{_cell(subject)} | "
                    f"{_cell(choice.decision)} | {_cell(choice.reason, limit=500)} |"
                )
            if source and source.unresolved_calls:
                lines.extend(["", f"源码中另有 {len(source.unresolved_calls)} 个无法静态确认的调用点；"
                              "它们不能仅凭名称被认定为已证实的函数关联。"])
    else:
        lines.append("没有生成出可用的测试计划。")

    lines.extend([
        "",
        "## 各轮结果",
        "",
        "| 轮次 | 候选校验 | 执行结果 | 通过 / 收集 | 行覆盖率 | 分支覆盖率 | 是否选中 |",
        "|---:|---|---|---:|---:|---:|---|",
    ])
    for item in state.history:
        run = item.execution
        cov = item.coverage
        lines.append(
            f"| {item.number} | {'失败：' + _cell(item.validation_error) if item.validation_error else '通过'} | "
            f"{_cell(run.kind) if run else '未执行'} | "
            f"{_number(run.passed if run else None)} / {_number(run.collected if run else None)} | "
            f"{_percent(cov.line_percent if cov else None)} | {_percent(cov.branch_percent if cov else None)} | "
            f"{'是' if item.number == state.best_available_round else '否'} |"
        )

    lines.extend(["", "## 问题与失败证据", ""])
    if state.issues:
        for issue in state.issues:
            location = f"（第 {issue.round_number} 轮）" if issue.round_number else ""
            lines.append(f"- **{_cell(issue.kind)}{location}**：{_cell(issue.summary, limit=500)}")
            lines.extend(f"  - 证据：{_cell(item, limit=500)}" for item in issue.evidence)
    else:
        lines.append("未记录未解决问题。")
    if execution and execution.failures:
        lines.extend(["", "测试运行观察到的失败：", ""])
        for failure in execution.failures:
            lines.append(
                f"- `{_cell(failure.nodeid)}`：{_cell(failure.exception_type)}，阶段 `{failure.phase}`"
            )

    usage = state.token_usage
    lines.extend([
        "",
        "## 模型调用与资源使用",
        "",
        f"- API 调用次数：{state.api_calls}",
        f"- 输入 Token：{_number(usage.prompt_tokens if usage else None)}",
        f"- 输出 Token：{_number(usage.completion_tokens if usage else None)}",
        f"- Token 总计：{_number(usage.total_tokens if usage else None)}",
        "",
        "## 数据结论",
        "",
    ])
    if state.stop_reason == StopReason.COVERAGE_TARGET_MET:
        lines.extend([
            f"1. 最终测试集的实测通过率为 {_percent(metrics['pass_percent'])}，没有观察到失败或执行错误。",
            f"2. 行覆盖率为 {_percent(metrics['line_percent'])}；"
            + ("源码不存在可统计的条件分支，因此分支覆盖率不适用。" if coverage and coverage.total_branches == 0
               else f"分支覆盖率为 {_percent(metrics['branch_percent'])}。"),
            f"3. 测试计划覆盖 {summary['scope']['planned_scenarios']} 个规格场景，其中 "
            f"{summary['scope']['unit_scenarios']} 个单元场景、"
            f"{summary['scope']['collaboration_scenarios']} 个协作场景。",
            "4. 以上数据支持“当前实现通过本报告所列场景，并达到配置的结构覆盖目标”。"
            "覆盖率和有限样例不能单独证明所有可能输入都正确。",
        ])
    else:
        lines.extend([
            f"1. 本次任务结论为“{conclusion['title']}”。",
            f"2. 直接原因：{_cell(conclusion['detail'], limit=800)}",
            "3. 在问题解决并重新运行前，不应把当前结果解释为被测代码已经通过完整验证。",
        ])

    functions = ", ".join(item.name for item in source.functions) if source else "未知"
    lines.extend([
        "",
        "## 产物索引",
        "",
        f"- 分析到的函数：{functions}",
        "- 最终测试：`test_subject.py`" if selected else "- 最终测试：未生成",
        "- 机器可读摘要：`summary.json`",
        "- 完整状态：`state.json`",
        "- 执行轨迹：`trace.jsonl`",
        "",
    ])
    return "\n".join(lines)
