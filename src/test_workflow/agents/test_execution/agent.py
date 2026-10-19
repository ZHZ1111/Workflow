"""One gated pytest/coverage invocation and an evidence-based action proposal."""

from collections.abc import Callable

from test_workflow.agents.test_generation.core.models import AgentAction, TaskStatus
from test_workflow.agents.test_generation.core.tools import ExecutionArtifactError, ExecutionWorkspaceError, run_tests_with_coverage

from ...adapters.execution import (
    ExecutionGateError, evaluate, preflight_execution, save_assessment, validate_observation,
)
from ...contracts import AgentContext, AgentResult, Message, MessagePayload, StatePatch
from ...storage import task_path


class TestExecutionAgent:
    __test__ = False

    def __init__(self, executor: Callable | None = None):
        if executor is not None and not callable(executor):
            raise TypeError("executor must be callable")
        self.executor = executor or run_tests_with_coverage
        self.observation_mode = "real" if executor is None else "injected"

    def run(self, context: AgentContext) -> AgentResult:
        def failed(code):
            diagnostics = ()
            # A host write/parse failure may leave useful raw evidence. Expose
            # only contained host directories, without trusting exception text.
            if attempted:
                try:
                    base = task_path(context.run_dir, f"agent_workspace/rounds/{context.round:02d}/executions", must_exist=False)
                    diagnostics = tuple(path.relative_to(context.run_dir).as_posix() for path in sorted(base.glob("run-*"))
                                        if path.is_dir() and not path.is_symlink() and not path.is_junction()
                                        and path.resolve().is_relative_to(context.run_dir.resolve()))
                except (OSError, ValueError):
                    pass
            return AgentResult(run_id=context.run_id, round=context.round, agent_id=context.agent_id,
                               status="error", error_code=code, diagnostic_paths=diagnostics)
        attempted = False
        try:
            context = AgentContext.model_validate_json(context.canonical_json())
            candidate, gate_error = preflight_execution(context)
            business = context.state.agent_state
            evidence = ()
            tool_calls = 0
            integrity_error = None
            if not gate_error and candidate.generated is not None and candidate.generated.code is not None and not candidate.generated.questions:
                attempted = True
                observed = self.executor(business.request.output_dir,
                                         timeout=business.request.execution_timeout_seconds, round_number=context.round)
                tool_calls = 1
                candidate, evidence = validate_observation(context, observed)
                expected = business.source_info.source_code.encode(business.source_info.encoding)
                if (business.request.source_path.read_bytes() != expected
                        or (business.request.output_dir / "subject.py").read_bytes() != expected):
                    integrity_error = "source_changed_during_execution"
                elif candidate.test_path.read_text(encoding="utf-8") != candidate.generated.code:
                    integrity_error = "tests_changed_during_execution"
            assessment = evaluate(context, candidate, gate_error, evidence, tool_calls=tool_calls,
                                  observation_mode=self.observation_mode, integrity_error=integrity_error)
            reference = save_assessment(context, assessment)
            messages = ()
            if assessment.action in {AgentAction.REPAIR_GENERATED_TEST, AgentAction.ADD_MISSING_CASES}:
                messages = (Message(message_id=f"msg-feedback-{context.round:02d}", run_id=context.run_id,
                                    round=context.round, sender=context.agent_id, receiver="test_generation",
                                    type="execution_feedback", payload=MessagePayload(
                                        candidate_id=assessment.candidate_id, artifact=reference,
                                        reason=assessment.reason, recommended_action=assessment.action,
                                        feedback=assessment.feedback)),)
            elif context.state.config.quality_review_enabled and assessment.task_status == TaskStatus.COMPLETED:
                messages = (Message(message_id=f"msg-review-ready-{context.round:02d}", run_id=context.run_id,
                                    round=context.round, sender=context.agent_id, receiver="test_quality_review",
                                    type="review_ready", payload=MessagePayload(
                                        candidate_id=assessment.candidate_id, artifact=reference,
                                        reason="Passing execution and target coverage are ready for quality review.",
                                        recommended_action=AgentAction.FINISH)),)
            return AgentResult(run_id=context.run_id, round=context.round, agent_id=context.agent_id,
                               patch=StatePatch(candidate=candidate if tool_calls else None,
                                                issues=tuple(item for item in assessment.feedback.issues if item not in business.issues),
                                                next_action=assessment.action),
                               messages=messages, artifacts=(*evidence, reference), assessment=assessment)
        except ExecutionGateError as error:
            return failed(error.code)
        except ExecutionWorkspaceError:
            return failed("execution_workspace_error")
        except ExecutionArtifactError:
            return failed("execution_artifact_error")
        except OSError:
            return failed("execution_io_error")
        except (ValueError, TypeError):
            return failed("execution_contract_error")
        except Exception:
            return failed("execution_internal_error")
