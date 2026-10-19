"""Collect P5 evidence; offline by default, explicit bounded real CLI mode."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import xml.etree.ElementTree as ET

from test_workflow.agents.test_generation.core.llm_client import LLMGenerationResult
from test_workflow.contracts import AgentContext, AgentResult, ExecutionAssessment
from test_workflow.events import Event
from test_workflow.state import StateStore
from test_workflow.storage import atomic_write, file_digest, verify_artifact

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/fixtures"
INDEX = ROOT / "evaluation/acceptance.json"

OFFLINE_CASES = (
    ("passed", "calculation", ("calculation_complete.json",), 3, "completed", "coverage_target_met"),
    ("feedback", "validation", ("validation_round_01.json", "validation_round_02.json"), 3, "completed", "coverage_target_met"),
    ("syntax_repaired", "calculation", ("calculation_invalid_syntax.json", "calculation_complete.json"), 3, "completed", "coverage_target_met"),
    ("name_repaired", "calculation", ("p3/name_error.json", "calculation_complete.json"), 3, "completed", "coverage_target_met"),
    ("three_round_limit", "calculation", ("calculation_invalid_syntax.json", "p4/invalid_round_02.json", "p4/invalid_round_03.json"), 3, "failed", "round_limit_reached"),
    ("partial_limit", "validation", ("validation_round_01.json",), 1, "partial", "round_limit_reached"),
    ("duplicate", "validation", ("validation_round_01.json", "validation_round_01.json"), 3, "partial", "no_progress"),
    ("no_coverage_progress", "validation", ("validation_round_01.json", "p4/no_coverage_progress.json"), 3, "partial", "no_progress"),
    ("removed_passing_test", "validation", ("validation_round_01.json", "validation_removed_pass.json"), 2, "partial", "round_limit_reached"),
    ("weakened_passing_test", "validation", ("validation_round_01.json", "validation_weakened_pass.json"), 2, "partial", "round_limit_reached"),
    ("later_execution_error", "validation", ("validation_round_01.json", "p4/later_name_error.json"), 2, "partial", "round_limit_reached"),
    ("suspected_defect", "discount", ("discount_defect.json",), 3, "needs_review", "suspected_source_defect"),
    ("ambiguous_assertion", "calculation", ("p3/ambiguous_assertion.json",), 3, "needs_review", "ambiguous_specification"),
    ("timeout", "execution/timeout", ("calculation_complete.json",), 3, "failed", "execution_timeout"),
    ("empty_candidate", "calculation", ("p3/no_tests_rejected.json",), 1, "failed", "round_limit_reached"),
)


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def relative(path):
    return path.resolve().relative_to(ROOT).as_posix()


def run_cli(source, directory, fixtures=(), *, env_file=None, budget=3, timeout=30):
    command = [sys.executable, "-m", "test_workflow", "run", "--source", str(ROOT / f"examples/{source}.py"),
               "--spec", str(ROOT / f"examples/{source}.md"), "--output", str(directory),
               "--max-rounds", str(budget), "--timeout", str(timeout)]
    if env_file is None:
        command.append("--demo")
        for fixture in fixtures:
            command += ["--fixture", str(FIXTURES / fixture)]
    else:
        command += ["--env-file", str(env_file)]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    atomic_write(directory.parent / f"{directory.name}_cli.txt", result.stdout + result.stderr)
    return result.returncode


def collect(directory):
    """Validate committed artifacts and every real handoff, without replaying nodes."""
    directory = directory.resolve(strict=True)
    state = StateStore(directory).load()
    business = state.agent_state
    assert state.workflow_status in {"finished", "error"} and state.finished_at is not None
    source_digest = hashlib.sha256(business.source_info.source_code.encode(business.source_info.encoding)).hexdigest()
    specification = business.request.spec_path.read_text(encoding="utf-8")
    assert file_digest(business.request.source_path) == source_digest
    assert specification == business.behavior_specification
    for reference in state.artifacts:
        verify_artifact(directory, reference)
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert (summary["run_id"], summary["task_status"], summary["selected_round"]) == (
        state.run_id, state.task_status, state.selected_round)
    if summary["final_test_file"]:
        final = Path(summary["final_test_file"])
        assert file_digest(final) == summary["final_test_sha256"] == file_digest(business.history[state.selected_round - 1].test_path)
        assert source_digest == file_digest(final.parent / "subject.py")
    events = tuple(Event.model_validate_json(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines())
    assert [item.seq for item in events] == list(range(1, state.last_event_seq + 1))
    rounds, pytest_count = [], 0
    for candidate in business.history:
        number = candidate.number
        gdir = directory / f"node_results/{number:02d}/test_generation"
        edir = directory / f"node_results/{number:02d}/test_execution"
        context = AgentContext.model_validate_json((gdir / "context.json").read_text(encoding="utf-8"))
        generated = AgentResult.model_validate_json((gdir / "result.json").read_text(encoding="utf-8"))
        assessed = AgentResult.model_validate_json((edir / "result.json").read_text(encoding="utf-8"))
        assessment = ExecutionAssessment.model_validate_json((directory / f"evaluations/{number:02d}/assessment.json").read_text(encoding="utf-8"))
        assert assessment == assessed.assessment and assessment.feedback.execution == candidate.execution
        assert assessment.feedback.coverage == candidate.coverage and assessment.observation_mode == "real"
        execution_context = AgentContext.model_validate_json((edir / "context.json").read_text(encoding="utf-8"))
        assert execution_context.inbox == generated.messages
        if number > 1:
            message = context.inbox[0]
            previous = ExecutionAssessment.model_validate_json((directory / f"evaluations/{number - 1:02d}/assessment.json").read_text(encoding="utf-8"))
            assert message.round == number - 1 and message.payload.feedback == previous.feedback
            assert message.message_id in state.consumed_message_ids
        if candidate.execution:
            assert candidate.test_path.read_text(encoding="utf-8") == candidate.generated.code
            assert len(tuple((directory / f"agent_workspace/rounds/{number:02d}/executions").glob("run-*"))) == 1
        pytest_count += assessment.pytest_started
        llm = LLMGenerationResult.model_validate_json((directory / f"agent_workspace/rounds/{number:02d}/llm_result.json").read_text(encoding="utf-8"))
        assert generated.generation.client_mode == ("real" if state.config.mode == "real" else "offline")
        assert llm.requested_model == business.request.model.model
        assert generated.generation.request_attempts <= generated.generation.max_request_attempts
        rounds.append({
            "round": number, "client_mode": generated.generation.client_mode,
            "request_attempts": generated.generation.request_attempts,
            "external_requests": generated.generation.external_requests,
            "format_repairs": generated.generation.format_repairs, "model_latency_seconds": llm.latency_seconds,
            "token_usage": llm.total_usage.model_dump(mode="json") if llm.total_usage else None,
            "response_model": llm.response_model, "response_ids": [item.response_id for item in llm.calls],
            "validation_error": candidate.validation_error,
            "execution_kind": candidate.execution.kind if candidate.execution else None,
            "collected": candidate.execution.collected if candidate.execution else None,
            "passed": candidate.execution.passed if candidate.execution else None,
            "failed": candidate.execution.failed if candidate.execution else None,
            "errors": candidate.execution.errors if candidate.execution else None,
            "coverage_status": candidate.coverage.status if candidate.coverage else None,
            "line_percent": candidate.coverage.line_percent if candidate.coverage else None,
            "branch_percent": candidate.coverage.branch_percent if candidate.coverage else None,
            "recommendation": assessment.recommendation, "stop_reason": assessment.stop_reason,
            "protected_tests": list(assessment.feedback.protected_tests),
            "issue_kinds": [item.kind for item in assessment.feedback.issues],
            "assessment": relative(directory / f"evaluations/{number:02d}/assessment.json"),
        })
    return {
        "run_id": state.run_id, "run_dir": relative(directory), "mode": state.config.mode,
        "model": business.request.model.model, "service": business.request.model.base_url,
        "started_at": state.started_at.isoformat(), "finished_at": state.finished_at.isoformat(),
        "duration_seconds": (state.finished_at - state.started_at).total_seconds(),
        "workflow_status": state.workflow_status, "task_status": state.task_status, "stop_reason": state.stop_reason,
        "max_rounds": state.config.max_rounds, "timeout": state.config.execution_timeout_seconds,
        "selected_round": state.selected_round, "selection_reason": summary["selection_reason"],
        "selected_all_passed": summary["selected_all_passed"], "latest_all_passed": summary["latest_all_passed"],
        "generation_calls": summary["generation_calls"], "external_requests": summary["external_requests"],
        "pytest_processes": pytest_count, "feedback_messages": sum(item.type == "execution_feedback" for item in state.messages),
        "source_sha256": source_digest, "spec_sha256": file_digest(business.request.spec_path),
        "source_unchanged": True, "spec_unchanged": True, "rounds": rounds,
        "report": relative(directory / "report.md"), "summary": relative(directory / "summary.json"),
    }


def run_tests(root):
    checks = {}
    for name, targets in (
        ("workflow", ["tests"]),
        ("vendor", ["tests/agentdev/test_agent.py", "tests/agentdev/test_generation.py",
                    "tests/agentdev/test_test_runner.py", "tests/agentdev/test_coverage_runner.py",
                    "tests/agentdev/test_failure_analysis.py", "tests/agentdev/test_llm_client.py"]),
    ):
        xml = ROOT / f"evaluation/p5_{name}_tests.xml"
        result = subprocess.run([sys.executable, "-m", "pytest", *targets, "-q", "--basetemp", str(root / name),
                                 "--junitxml", str(xml)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        atomic_write(ROOT / f"evaluation/p5_{name}_tests.txt", result.stdout + result.stderr)
        suites = ET.parse(xml).getroot().findall("testsuite")
        checks[name] = {field: sum(int(suite.attrib.get(field, 0)) for suite in suites)
                        for field in ("tests", "failures", "errors", "skipped")}
        checks[name].update(exit_code=result.returncode, junit=relative(xml))
        print(result.stdout.strip(), flush=True)
    return checks


def matrix(evidence):
    records = []
    check = evidence["checks"].get("workflow", {})
    if check.get("junit") and (ROOT / check["junit"]).exists():
        for item in ET.parse(ROOT / check["junit"]).getroot().iter("testcase"):
            records.append({"junit": check["junit"], "case": item.attrib["classname"] + "." + item.attrib["name"],
                            "passed": not any(item.find(tag) is not None for tag in ("failure", "error", "skipped"))})

    def tests(pattern):
        found = [item for item in records if pattern in item["case"]]
        return bool(found) and all(item["passed"] for item in found), found

    def offline(*names):
        return all(evidence["offline"].get(name, {}).get("matches_expected", False) for name in names)

    def real(name, status):
        sample = evidence["real"].get(name, {})
        return sample.get("task_status") == status and bool(sample.get("rounds"))

    definitions = (
        ("A01", "正确简单函数", real("calculation", "completed"), ["real.calculation"], None),
        ("A02", "多分支目标覆盖", real("validation", "completed"), ["real.validation"], None),
        ("A03", "真实执行反馈补充", offline("feedback"), ["offline.feedback"], "test_real_feedback_is_consumed_and_changes_next_prompt"),
        ("A04", "可修复候选错误", offline("syntax_repaired", "name_repaired"), ["offline.syntax_repaired", "offline.name_repaired"], None),
        ("A05", "规格支持的边界缺陷", real("discount", "needs_review"), ["real.discount"], None),
        ("A06", "不明确规格需复核", offline("ambiguous_assertion"), ["offline.ambiguous_assertion"], "test_blocking_question_goes_to_review_without_pytest"),
        ("A07", "真实超时有界停止", offline("timeout"), ["offline.timeout"], "test_timeout_terminates_pytest_and_keeps_source_and_diagnostics"),
        ("A08", "API/非法响应有界失败", True, [], "test_provider_retry_exhaustion_is_bounded"),
        ("A09", "空测试禁止完成", offline("empty_candidate"), ["offline.empty_candidate"], "test_no_tests_real_backend_observation_cannot_complete"),
        ("A10", "预算/无进展与选择", offline("three_round_limit", "duplicate", "no_coverage_progress", "later_execution_error"),
         ["offline.three_round_limit", "offline.duplicate", "offline.no_coverage_progress", "offline.later_execution_error"], None),
        ("A11", "错误任务或轮次拒绝", True, [], "test_corrupt_first_generation_cannot_launch_execution"),
        ("A12", "节点注册/定义扩展", True, [], "test_registered_trailing_node_runs_without_loop_edits"),
        ("A13", "已有输出不覆盖", True, [], "test_cli_real_execution_offline_generation_and_exact_output"),
        ("A14", "覆盖导出失败保留测试", True, [], "test_coverage_export_failure_keeps_tests_without_completion"),
        ("A15", "保护历史通过行为", offline("removed_passing_test", "weakened_passing_test"),
         ["offline.removed_passing_test", "offline.weakened_passing_test"], None),
        ("W11", "跨节点与修订归属", True, [], "test_wrong_revision_round_preserves_committed_history"),
        ("W12", "重复消息不重复执行", True, [], "test_corrupt_first_generation_cannot_launch_execution[duplicate]"),
        ("W13", "中文/空格输入输出", True, [], "test_chinese_and_space_input_paths_use_same_real_workflow"),
        ("W14", "非法输入零调用", True, [], "test_invalid_input_cannot_spend_generation_call"),
        ("W15", "副本变更阻止执行", True, [], "test_changed_subject_between_nodes_is_rejected_before_pytest"),
        ("W16", "持久化失败不伪称保存", True, [], "test_persistent_state_write_failure_exposes_last_saved_snapshot"),
    )
    rows = []
    for identity, label, observed, references, pattern in definitions:
        passed, cases = tests(pattern) if pattern else (True, [])
        rows.append({"id": identity, "name": label, "status": "passed" if observed and passed else "pending_or_failed",
                     "run_evidence": references, "test_evidence": cases})
    rows += [
        {"id": "W17", "name": "正式 ResultSummaryNode 示例", "status": "delivery_pending_P6",
         "note": "A12 已证明接口接入；正式示例与文档按 P6 交付。"},
        {"id": "W18", "name": "干净目录安装", "status": "pending_P7"},
    ]
    return rows


def write_report(evidence):
    checks = evidence["checks"]
    lines = ["# P5 系统与真实模型验收", "", f"更新时间：{evidence['updated_at']}（北京时间）。",
             f"P5 完成状态：`{evidence['p5_complete']}`；旧项目基线与 P1–P4 记录仍独立保留。", "",
             "## 本次真实模型", "", "配置服务为 https://api.deepseek.com，模型名称以请求与响应实测为准。",
             "三个必需样例的源码、规格、契约和执行反馈已获用户明确外传授权；密钥仅用于认证，不进入产物。", "",
             "| 样例 | 业务结论 | 停止原因 | 轮数 | 模型请求 | 耗时/秒 | 通过/失败 | 行/分支覆盖 |",
             "|---|---|---|---|---|---|---|---|"]
    for name, sample in evidence["real"].items():
        last = sample["rounds"][-1] if sample["rounds"] else {}
        lines.append(f"| {name} | {sample['task_status']} | {sample['stop_reason']} | {len(sample['rounds'])} | "
                     f"{sample['external_requests']} | {sample['duration_seconds']:.3f} | {last.get('passed')}/{last.get('failed')} | "
                     f"{last.get('line_percent')}/{last.get('branch_percent')} |")
    lines += ["", "正常样例和多分支样例本次首轮达标；这不证明真实模型发生了反馈提升。受控离线反馈单独证明自动循环。",
              "discount 保留 100→90 的规格断言，本次真实测试失败后进入 suspected_source_defect，未修改预期或原始源码。",
              "validation 的请求数含客户端格式修复请求；模型请求数、格式修复与候选轮数分别记录。", "",
              "可选同文件关系样例未获本次外传授权，因此未执行；正常样例重复稳定性测试也未执行，不作稳定性结论。", "",
              "## 本次离线与系统回归", "",
              f"离线生成 + 真实执行的正式 CLI 案例：{len(evidence['offline'])} 个；"
              f"pytest 子进程 {sum(item['pytest_processes'] for item in evidence['offline'].values())} 次；外部请求为 0。", "",
              "| 回归 | 测试数 | 失败 | 错误 | 跳过 | 退出码 |", "|---|---|---|---|---|---|"]
    for name, result in checks.items():
        lines.append(f"| {name} | {result['tests']} | {result['failures']} | {result['errors']} | {result['skipped']} | {result['exit_code']} |")
    lines += ["", "新增系统测试覆盖错误任务/轮次/节点/接收者、重复消息、文件摘要、错误修订保留历史、源码副本变更、持久化写入失败、阻塞规格疑问和中文/空格输入。",
              "非法路径、空规格、不支持类和非法 UTF-8 均在生成调用前失败。磁盘持续写入失败时向调用方抛错并保留最后成功快照，不保证新 error 状态可以落盘。", "",
              "空候选在生产门禁被拒；底层 no_tests 的真实观察使用单独受控工作区验证，未放宽生产门禁。API 错误、覆盖导出失败和持久化失败用本地 Mock/故障注入，不计为真实模型流量。", "",
              f"原始源码和规格在全部 CLI 案例中摘要一致；vendor 快照 {evidence['vendor_manifest_files']} 文件未变：`{evidence['vendor_files_unchanged']}`。", "",
              "## 核心矩阵", "", "| ID | 检查 | 结果 |", "|---|---|---|"]
    for row in evidence["matrix"]:
        lines.append(f"| {row['id']} | {row['name']} | {row['status']} |")
    lines += ["", "A01–A15 对应 TASK_CHECKLIST 第 8 节；W11–W16 补充 docs/test-plan 的系统边界。W01–W10 对应 A01–A10/A14/A15 的运行证据。",
              "A12 已由统一接口的尾部节点测试证明；正式 ResultSummaryNode 示例属于 P6，干净安装属于 P7。", "",
              "## 复现与证据", "", "```powershell", ".\\.venv\\Scripts\\python.exe scripts/p5_acceptance.py --offline --tests",
              "# 只读取已有真实任务，不调用模型", ".\\.venv\\Scripts\\python.exe scripts/p5_acceptance.py --collect-real <calculation目录> <validation目录> <discount目录>",
              "# 校验索引中已有的离线/真实任务并刷新报告，不调用模型", ".\\.venv\\Scripts\\python.exe scripts/p5_acceptance.py --refresh",
              "# 使用本地配置重新发起三个有界真实案例", ".\\.venv\\Scripts\\python.exe scripts/p5_acceptance.py --real --env-file .env", "```", "",
              "[机器可读结果索引](acceptance.json) 保存任务目录、run_id、输入摘要、每轮模型用量/格式修复/耗时/执行/覆盖/动作，以及矩阵对应的实际 JUnit testcase。",
              "每次独立目录保留 state.json、Context/Result、events/messages、assessment、原始 pytest/JUnit/coverage 和最终测试；完整 outputs/ 不提交，精简索引与验收证据提交。", ""]
    for name, sample in evidence["real"].items():
        lines.append(f"- {name}：[{sample['run_id']}](../{sample['report']})，模型 `{sample['model']}`。")
    atomic_write(ROOT / "evaluation/acceptance.md", "\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="Run fresh deterministic CLI cases")
    parser.add_argument("--tests", action="store_true", help="Run system regression and relevant vendor tests")
    parser.add_argument("--real", action="store_true", help="Send the three mandatory samples to configured service")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--collect-real", type=Path, nargs="*", help="Read existing real runs without sending requests")
    parser.add_argument("--refresh", action="store_true", help="Revalidate indexed runs and rebuild report without requests")
    args = parser.parse_args()
    if not (args.offline or args.tests or args.real or args.collect_real or args.refresh):
        parser.error("select --offline, --tests, --real, --collect-real or --refresh")
    evidence = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {
        "schema_version": "1.0", "phase": "P5", "offline": {}, "real": {}, "checks": {},
        "optional": {"relationships": "not_run_not_authorized", "normal_repeat": "not_run"},
    }
    evidence["updated_at"] = now()
    root = ROOT / "outputs/p5" / f"acceptance-{uuid4().hex[:10]}"
    root.mkdir(parents=True, exist_ok=False)
    if args.refresh:
        for mode in ("offline", "real"):
            for name, old in tuple(evidence[mode].items()):
                updated = collect(ROOT / old["run_dir"])
                for field in ("cli_exit_code", "matches_expected"):
                    if field in old:
                        updated[field] = old[field]
                evidence[mode][name] = updated
    if args.offline:
        cases = {}
        for name, source, fixtures, budget, status, stop in OFFLINE_CASES:
            directory = root / name
            code = run_cli(source, directory, fixtures, budget=budget, timeout=1 if name == "timeout" else 30)
            sample = collect(directory)
            sample["cli_exit_code"] = code
            sample["matches_expected"] = (code, sample["workflow_status"], sample["task_status"], sample["stop_reason"]) == (
                0 if status == "completed" else 1, "finished", status, stop)
            cases[name] = sample
            evidence["offline"] = cases
            atomic_write(INDEX, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
            print(f"offline {name}: {sample['task_status']}/{sample['stop_reason']}; expected={sample['matches_expected']}", flush=True)
    if args.real:
        for source in ("calculation", "validation", "discount"):
            directory = root / f"real-{source}"
            code = run_cli(source, directory, env_file=args.env_file)
            sample = collect(directory)
            sample["cli_exit_code"] = code
            evidence["real"][source] = sample
            atomic_write(INDEX, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
            print(f"real {source}: {sample['task_status']}/{sample['stop_reason']}", flush=True)
    if args.collect_real:
        for directory in args.collect_real:
            sample = collect(directory)
            if sample["mode"] != "real":
                raise ValueError("real collection requires real mode evidence")
            name = StateStore(directory).load().agent_state.request.source_path.stem
            evidence["real"][name] = sample
    if args.tests:
        evidence["checks"] = run_tests(root)
    manifest = json.loads((ROOT / "evaluation/reuse_manifest.json").read_text(encoding="utf-8"))
    from vendor_audit import audit_vendor
    evidence["vendor_files_unchanged"] = audit_vendor(ROOT)["unchanged"]
    evidence["vendor_manifest_files"] = len(manifest["files"])
    evidence["vendor_files_verified"] = audit_vendor(ROOT)["verified"]
    expected = {"calculation": ("completed", "coverage_target_met"), "validation": ("completed", "coverage_target_met"),
                "discount": ("needs_review", "suspected_source_defect")}
    evidence["real_acceptance_complete"] = all(name in evidence["real"] and (
        evidence["real"][name]["task_status"], evidence["real"][name]["stop_reason"]) == outcome
        and evidence["real"][name]["workflow_status"] == "finished"
        and evidence["real"][name]["external_requests"] is not None
        and evidence["real"][name]["external_requests"] > 0
        for name, outcome in expected.items())
    evidence["offline_acceptance_complete"] = len(evidence["offline"]) == len(OFFLINE_CASES) and all(
        item["matches_expected"] for item in evidence["offline"].values())
    evidence["regression_complete"] = bool(evidence["checks"]) and all(item["exit_code"] == 0 for item in evidence["checks"].values())
    evidence["matrix"] = matrix(evidence)
    evidence["core_matrix_complete"] = all(item["status"] == "passed" for item in evidence["matrix"] if item["id"] not in {"W17", "W18"})
    evidence["p5_complete"] = (evidence["real_acceptance_complete"] and evidence["offline_acceptance_complete"]
                               and evidence["regression_complete"] and evidence["vendor_files_verified"] and evidence["core_matrix_complete"])
    evidence["updated_at"] = now()
    atomic_write(INDEX, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    write_report(evidence)
    success = (evidence["vendor_files_verified"] and (not args.tests or evidence["regression_complete"])
               and (not args.offline or evidence["offline_acceptance_complete"])
               and (not (args.real or args.collect_real) or evidence["real_acceptance_complete"]))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
