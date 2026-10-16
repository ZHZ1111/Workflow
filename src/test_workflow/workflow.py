"""Single-writer coordinator for sequential nodes and bounded feedback."""

from pathlib import Path
from time import perf_counter
from uuid import uuid4

from testgen_agent.agent import choose_action, select_best_candidate
from testgen_agent.models import AgentAction, AgentState, CandidateRound, ModelInfo, OracleMode, StopReason, TaskInput, TaskStatus
from testgen_agent.tools import analyze_source

from .config import WorkflowConfig
from .contracts import AgentContext, AgentResult, StatePatch, ExecutionAssessment, QualityAssessment
from .definitions import WorkflowDefinition
from .events import EventWriter, MessageWriter
from .registry import AgentRegistry
from .quality_routing import validate_quality_result
from .state import StateStore, WorkflowState, utc_now
from .storage import atomic_write, verify_artifact


class HandoffError(ValueError):
    """A node output cannot be committed to this workflow."""


def create_run(*, source: Path, specification: Path, output: Path,
               config: WorkflowConfig | None = None, exact_output: bool = False) -> WorkflowState:
    config = config or WorkflowConfig()
    source = source.resolve(strict=True)
    specification = specification.resolve(strict=True)
    # Validate inputs before creating a task or invoking any node.
    facts = analyze_source(source)
    spec = specification.read_text(encoding="utf-8")
    if not spec.strip():
        raise ValueError("a nonempty UTF-8 behavior specification is required")
    run_id = f"run-{uuid4().hex}"
    if output.is_symlink() or output.is_junction():
        raise ValueError("output cannot be a link")
    root = output.resolve() if exact_output else output.resolve() / run_id
    if root.exists():
        raise FileExistsError("run directory already exists")
    request = TaskInput(source_path=source, spec_path=specification,
                        output_dir=root / "agent_workspace", max_rounds=config.max_rounds,
                        execution_timeout_seconds=config.execution_timeout_seconds,
                        model=config.model or ModelInfo(model="offline-fixture-model", base_url="https://example.com"))
    business = AgentState(schema_version="1.1", run_id=run_id, request=request,
                          oracle_mode=OracleMode.SPECIFICATION,
                          behavior_specification=spec, source_info=facts)
    state = WorkflowState(run_id=run_id, run_dir=root, config=config, agent_state=business)
    root.mkdir(parents=True, exist_ok=False)
    atomic_write(root / "run.json", state.canonical_json(indent=2) + "\n")
    StateStore(root).save(state)
    return state


class WorkflowRunner:
    def __init__(self, registry: AgentRegistry, definition: WorkflowDefinition):
        self.registry = registry
        self.definition = definition
        for node in definition.nodes:
            registry.get(node.agent_id)

    def _inbox(self, state: WorkflowState, node):
        pending = tuple(message for message in state.messages if message.receiver == node.agent_id
                        and message.message_id not in state.consumed_message_ids)
        revision = node.agent_id == "test_generation" and state.current_round > 1
        sender, message_type, number = node.receives_from, None, state.current_round
        if revision:
            prior_quality = next((item for item in state.quality_assessments if item.round == state.current_round - 1), None)
            quality_revision = state.config.quality_review_enabled and prior_quality is not None and prior_quality.decision.verdict == "request_revision"
            sender = "test_quality_review" if quality_revision else "test_execution"
            message_type = "quality_feedback" if quality_revision else "execution_feedback"
            number -= 1
        elif node.agent_id == "test_execution":
            message_type = "candidate_ready"
        elif node.agent_id == "test_quality_review":
            message_type = "review_ready"
        blocked = (state.config.mode != "demo" and node.agent_id == "test_execution" and state.agent_state.history
                   and (state.agent_state.history[-1].validation_error or state.agent_state.history[-1].generated.questions))
        if sender is not None and not pending and not blocked:
            raise HandoffError("consumer did not receive its required handoff")
        if (sender is None and pending) or (pending and len(pending) != 1):
            raise HandoffError("consumer requires exactly one expected handoff")
        for message in pending:
            if (message.sender != sender or message.round != number
                    or (message_type is not None and message.type != message_type)):
                raise HandoffError("consumer received a foreign sender, type or source round")
            verify_artifact(state.run_dir, message.payload.artifact)
            if message_type == "quality_feedback":
                saved = QualityAssessment.model_validate_json(verify_artifact(
                    state.run_dir, message.payload.artifact).read_text(encoding="utf-8"))
                if (message.payload.artifact not in state.artifacts or saved != prior_quality
                        or saved.decision.verdict != "request_revision" or message.payload.reason != saved.decision.summary
                        or message.payload.quality_feedback.findings != tuple(item for item in saved.decision.findings if item.blocking)):
                    raise HandoffError("quality feedback differs from the committed source assessment")
            elif message_type == "execution_feedback" and message.payload.artifact.kind == "assessment":
                saved = ExecutionAssessment.model_validate_json(verify_artifact(
                    state.run_dir, message.payload.artifact).read_text(encoding="utf-8"))
                if saved.feedback != message.payload.feedback or saved.action != message.payload.recommended_action:
                    raise HandoffError("execution feedback differs from its assessment")
        return pending

    @staticmethod
    def _selected_candidate(state: WorkflowState):
        if (state.config.quality_review_enabled and state.quality_status == "passed"
                and state.quality_assessments and state.quality_assessments[-1].round == state.current_round):
            return state.current_round
        return select_best_candidate(state.agent_state.history)

    def _business_patch(self, state: WorkflowState, patch: StatePatch, result_artifacts):
        business = state.agent_state
        history = business.history
        candidate = patch.candidate
        if candidate is not None:
            if candidate.number != state.current_round:
                raise HandoffError("candidate belongs to another round")
            if candidate.number == len(history) + 1:
                if candidate.execution is not None or candidate.coverage is not None:
                    raise HandoffError("new candidate cannot already contain execution observations")
                history = (*history, candidate)
            elif candidate.number == len(history) and history:
                previous = history[-1]
                if (candidate.generated, candidate.test_path, candidate.validation_error) != (
                    previous.generated, previous.test_path, previous.validation_error,
                ) or previous.execution is not None:
                    raise HandoffError("evaluation cannot rewrite or execute a historical candidate twice")
                history = (*history[:-1], candidate)
            else:
                raise HandoffError("candidate cannot overwrite earlier history")
            if candidate.test_path is not None:
                path = candidate.test_path.resolve(strict=True)
                if not path.is_file() or not path.is_relative_to(state.run_dir):
                    raise HandoffError("candidate test file belongs outside this task")
                if not any(item.kind == "tests" and verify_artifact(state.run_dir, item) == path
                           for item in (*state.artifacts, *result_artifacts)):
                    raise HandoffError("candidate test file requires a verified tests artifact")
        return business.updated(
            history=history, current_round=len(history),
            issues=(*business.issues, *patch.issues),
            next_action=patch.next_action if patch.next_action is not None else business.next_action,
            api_calls=business.api_calls + patch.api_calls_delta,
            token_usage=patch.token_usage if patch.token_usage_reported or patch.token_usage is not None else business.token_usage,
        )

    def apply_result(self, state: WorkflowState, result: AgentResult,
                     consumed: tuple[str, ...] = ()) -> WorkflowState:
        """Validate a node proposal and return a new state; the coordinator persists it."""
        # Never trust model_copy(update=...) or a node returning an untyped dict.
        if not isinstance(result, AgentResult):
            raise HandoffError("node must return an AgentResult")
        result = AgentResult.model_validate_json(result.canonical_json())
        state = WorkflowState.model_validate_json(state.canonical_json())
        if (result.run_id, result.round, result.agent_id) != (
            state.run_id, state.current_round, state.current_node,
        ):
            raise HandoffError("result belongs to another task, round or node")
        if result.status != "ok":
            raise HandoffError("node returned an error result")
        node = next((node for node in self.definition.nodes if node.agent_id == result.agent_id), None)
        if node is None:
            raise HandoffError("result node is not declared in this workflow")
        if consumed and consumed != tuple(message.message_id for message in self._inbox(state, node)):
            raise HandoffError("consumption must match the current unique expected handoff")
        paths = {item.path for item in state.artifacts}
        for artifact in result.artifacts:
            if artifact.round != state.current_round or artifact.candidate_id != f"{state.run_id}:{state.current_round}":
                raise HandoffError("artifact belongs to another candidate")
            if artifact.path in paths:
                raise HandoffError("artifact overwrites a previously committed reference")
            paths.add(artifact.path)
            verify_artifact(state.run_dir, artifact)
        known_ids = {item.message_id for item in state.messages}
        artifacts = (*state.artifacts, *result.artifacts)
        for message in result.messages:
            if (message.run_id, message.round, message.sender) != (
                state.run_id, state.current_round, state.current_node,
            ):
                raise HandoffError("message belongs to another task, round or sender")
            destinations = [node for node in self.definition.nodes if node.agent_id == message.receiver]
            feedback_route = message.type in {"execution_feedback", "quality_feedback"} and any(
                route.sender == message.sender and route.receiver == message.receiver
                for route in self.definition.feedback_routes)
            if not destinations or (destinations[0].receives_from != state.current_node and not feedback_route):
                raise HandoffError("message receiver is not a declared consumer")
            if message.message_id in known_ids:
                raise HandoffError("message ID was already sent")
            known_ids.add(message.message_id)
            if message.payload.artifact not in artifacts:
                raise HandoffError("message refers to an undeclared artifact")
            verify_artifact(state.run_dir, message.payload.artifact)
        business = self._business_patch(state, result.patch, result.artifacts)
        if result.messages and (not business.history or business.history[-1].number != state.current_round):
            raise HandoffError("handoff requires a committed candidate for the source round")
        for message in result.messages:
            if message.type == "candidate_ready":
                reference = message.payload.artifact
                if reference.kind != "candidate":
                    raise HandoffError("candidate_ready requires a candidate artifact")
                saved = CandidateRound.model_validate_json(
                    verify_artifact(state.run_dir, reference).read_text(encoding="utf-8"))
                if saved != business.history[-1]:
                    raise HandoffError("candidate artifact differs from the proposed business state")
            elif message.type in {"execution_feedback", "review_ready"} and message.payload.artifact.kind == "assessment":
                saved = ExecutionAssessment.model_validate_json(verify_artifact(
                    state.run_dir, message.payload.artifact).read_text(encoding="utf-8"))
                if (saved != result.assessment or (message.type == "execution_feedback" and (
                        saved.feedback != message.payload.feedback or saved.action != message.payload.recommended_action))):
                    raise HandoffError("feedback differs from its recorded assessment")
        if result.assessment is not None:
            assessment = result.assessment
            candidate = business.history[-1]
            refs = tuple(item for item in result.artifacts if item.kind == "assessment")
            if (result.agent_id != "test_execution" or len(refs) != 1
                    or ExecutionAssessment.model_validate_json(verify_artifact(state.run_dir, refs[0]).read_text(encoding="utf-8")) != assessment
                    or assessment.evidence != tuple(item for item in result.artifacts if item.kind != "assessment")):
                raise HandoffError("assessment requires matching registered evidence")
            if (assessment.candidate_id != f"{state.run_id}:{state.current_round}"
                    or assessment.feedback.execution != candidate.execution
                    or assessment.feedback.coverage != candidate.coverage
                    or assessment.action != result.patch.next_action):
                raise HandoffError("assessment differs from the committed candidate or action")
            ready = state.config.quality_review_enabled and assessment.task_status == TaskStatus.COMPLETED
            if ready and (len(result.messages) != 1 or result.messages[0].type != "review_ready"):
                raise HandoffError("quality-enabled completion requires one exclusive review_ready")
            if not ready and any(message.type == "review_ready" for message in result.messages):
                raise HandoffError("review_ready requires a completed execution and enabled review")
        elif any(message.type == "review_ready" for message in result.messages):
            raise HandoffError("review_ready requires an execution assessment")
        quality_updates = {}
        accepted_messages = result.messages
        if result.quality_assessment is not None:
            inbox = self._inbox(state, node)
            stop = validate_quality_result(state, result, inbox)
            quality = result.quality_assessment
            verdict = quality.decision.verdict
            if any(message.payload.reason != quality.decision.summary for message in result.messages):
                raise HandoffError("quality feedback reason differs from its assessment")
            quality_updates = {
                "quality_assessments": (*state.quality_assessments, quality),
                "quality_status": {"approve": "passed", "request_revision": "changes_requested", "request_review": "needs_review"}[verdict],
                "quality_stop_reason": {"approve": "approved", "request_revision": stop or "quality_gaps",
                                        "request_review": "ambiguous_specification"}[verdict],
            }
            if verdict == "request_revision" and stop is None:
                business = business.updated(next_action=AgentAction.ADD_MISSING_CASES)
            if stop is not None:
                # Result messages are proposals; denied feedback is never journaled.
                accepted_messages = ()
        elif state.config.quality_review_enabled and result.patch.candidate is not None and result.agent_id == "test_generation":
            quality_updates = {"quality_status": "not_run", "quality_stop_reason": None}
        return state.updated(
            agent_state=business, artifacts=artifacts,
            messages=(*state.messages, *accepted_messages),
            consumed_message_ids=(*state.consumed_message_ids, *consumed),
            **quality_updates,
        )

    def run(self, initial: WorkflowState) -> WorkflowState:
        if initial.workflow_status != "pending":
            raise ValueError("only new pending tasks can run; persisted tasks are read-only")
        measured = initial.config.mode != "demo"
        if measured and (
            len(self.definition.nodes) < 2
            or tuple(node.agent_id for node in self.definition.nodes[:2]) != ("test_generation", "test_execution")
            or not any(route.sender == "test_execution" and route.receiver == "test_generation"
                       for route in self.definition.feedback_routes)
        ):
            raise ValueError("testing requires generation, execution and a declared feedback edge")
        review_nodes = tuple(node for node in self.definition.nodes if node.agent_id == "test_quality_review")
        if initial.config.quality_review_enabled:
            if (not measured or len(review_nodes) != 1 or len(self.definition.nodes) < 3
                    or self.definition.nodes[2] != review_nodes[0] or review_nodes[0].receives_from != "test_execution"
                    or not any(route.sender == "test_quality_review" and route.receiver == "test_generation"
                               for route in self.definition.feedback_routes)):
                raise ValueError("enabled quality review requires its declared node, dependency and feedback route")
        elif review_nodes:
            raise ValueError("a quality workflow requires quality_review_enabled")
        state = WorkflowState.model_validate_json(initial.canonical_json())
        store = StateStore(state.run_dir)
        events = EventWriter(state.run_dir, state.run_id)
        messages = MessageWriter(state.run_dir)
        node_started = None

        def save_event(event_type, **fields):
            nonlocal state
            events.append(event_type, **fields)
            state = state.updated(last_event_seq=events.seq)
            store.save(state)

        def invoke(node):
            nonlocal state, node_started
            state = state.updated(current_node=node.agent_id)
            node_path = f"node_results/{state.current_round:02d}/{node.agent_id}"
            save_event("node_started", node=node.agent_id, round=state.current_round,
                       context_path=f"{node_path}/context.json")
            started = perf_counter()
            node_started = started
            inbox = self._inbox(state, node)
            context = AgentContext(run_id=state.run_id, round=state.current_round,
                                   agent_id=node.agent_id, run_dir=state.run_dir,
                                   snapshot_json=state.canonical_json(), inbox=inbox)
            directory = state.run_dir / "node_results" / f"{state.current_round:02d}" / node.agent_id
            directory.mkdir(parents=True, exist_ok=False)
            atomic_write(directory / "context.json", context.canonical_json(indent=2) + "\n")
            result = self.registry.get(node.agent_id).run(context)
            if not isinstance(result, AgentResult):
                raise HandoffError("node must return an AgentResult")
            result = AgentResult.model_validate_json(result.canonical_json())
            atomic_write(directory / "result.json", result.canonical_json(indent=2) + "\n")
            save_event("node_output_saved", node=node.agent_id, round=state.current_round,
                       result_path=f"{node_path}/result.json")
            if (result.run_id, result.round, result.agent_id) != (state.run_id, state.current_round, state.current_node):
                raise HandoffError("result belongs to another task, round or node")
            if result.status == "error":
                if result.generation and result.generation.external_requests is not None:
                    state = state.updated(agent_state=state.agent_state.updated(
                        api_calls=state.agent_state.api_calls + result.generation.external_requests))
                raise NodeFailure(result.error_code)
            proposed = self.apply_result(state, result, tuple(item.message_id for item in inbox))
            if measured:
                selected = self._selected_candidate(proposed)
                proposed = proposed.updated(selected_round=selected,
                                            agent_state=proposed.agent_state.updated(best_available_round=selected))
            for message in proposed.messages[len(state.messages):]:
                messages.append(message)
                events.append("message_sent", node=node.agent_id, round=message.round,
                              message_id=message.message_id, candidate_id=message.payload.candidate_id,
                              artifact_path=message.payload.artifact.path)
            for message in inbox:
                events.append("message_consumed", node=node.agent_id, round=state.current_round,
                              message_id=message.message_id, candidate_id=message.payload.candidate_id,
                              artifact_path=message.payload.artifact.path)
            state = proposed.updated(last_event_seq=events.seq)
            save_event("node_finished", node=node.agent_id, round=state.current_round,
                       duration_seconds=perf_counter() - started, result_path=f"{node_path}/result.json")
            node_started = None
            return result

        def finalize(status, stop, action=AgentAction.FINISH, **quality_updates):
            nonlocal state
            selected = self._selected_candidate(state)
            if status == TaskStatus.COMPLETED and state.config.quality_review_enabled and state.quality_status != "passed":
                raise HandoffError("quality-enabled completion requires approval")
            state = state.updated(task_status=status, stop_reason=stop, selected_round=selected,
                                  agent_state=state.agent_state.updated(best_available_round=selected,
                                                                       stop_reason=stop, next_action=action), **quality_updates)

        try:
            state = state.updated(workflow_status="running", current_round=1)
            save_event("workflow_started")
            if not measured:
                for node in self.definition.nodes:
                    invoke(node)
            else:
                while True:
                    generated = invoke(self.definition.nodes[0])
                    candidate = state.agent_state.history[-1] if state.agent_state.history else None
                    if (candidate is None or candidate.number != state.current_round
                            or generated.patch.next_action not in {AgentAction.RUN_TESTS, AgentAction.REPAIR_GENERATED_TEST,
                                                                 AgentAction.REQUEST_REVIEW, AgentAction.FINISH}):
                        raise HandoffError("generation did not supply a routable candidate")
                    evaluated = invoke(self.definition.nodes[1])
                    assessment = evaluated.assessment
                    if assessment is None:
                        raise HandoffError("execution requires a structured assessment")
                    decision = choose_action(state.agent_state)
                    if assessment.integrity_error and (
                        assessment.integrity_error not in {"source_changed_during_execution", "tests_changed_during_execution"}
                        or assessment.action != AgentAction.FINISH
                        or assessment.stop_reason != StopReason.UNRECOVERABLE_ERROR
                        or assessment.task_status != (TaskStatus.PARTIAL if candidate.available or state.selected_round else TaskStatus.FAILED)
                    ):
                        raise HandoffError("invalid integrity stop recommendation")
                    if not assessment.integrity_error and (
                        assessment.action, assessment.stop_reason, assessment.task_status
                    ) != (decision.action, decision.stop_reason, decision.status):
                        raise HandoffError("execution recommendation differs from allowed transition")
                    save_event("route_selected", round=state.current_round, action=assessment.action.value,
                               stop_reason=assessment.stop_reason.value if assessment.stop_reason else None)
                    if assessment.action in {AgentAction.REPAIR_GENERATED_TEST, AgentAction.ADD_MISSING_CASES}:
                        if state.current_round >= state.config.max_rounds:
                            finalize(TaskStatus.PARTIAL if state.selected_round else TaskStatus.FAILED,
                                     StopReason.ROUND_LIMIT_REACHED)
                            break
                        if len(evaluated.messages) != 1 or evaluated.messages[0].type != "execution_feedback":
                            raise HandoffError("revision requires the previous assessment feedback")
                        state = state.updated(current_round=state.current_round + 1)
                        continue
                    if assessment.action not in {AgentAction.FINISH, AgentAction.REQUEST_REVIEW} or assessment.stop_reason is None:
                        raise HandoffError("unknown or incomplete terminal recommendation")
                    if state.config.quality_review_enabled and assessment.task_status == TaskStatus.COMPLETED:
                        reviewed = invoke(review_nodes[0])
                        verdict = reviewed.quality_assessment.decision.verdict
                        save_event("route_selected", node="test_quality_review", round=state.current_round,
                                   action={"approve": "quality_approved", "request_revision": "quality_revision_requested",
                                           "request_review": "quality_needs_review"}[verdict],
                                   stop_reason=state.quality_stop_reason)
                        if verdict == "approve":
                            finalize(TaskStatus.COMPLETED, StopReason.COVERAGE_TARGET_MET)
                        elif verdict == "request_review":
                            finalize(TaskStatus.NEEDS_REVIEW, StopReason.AMBIGUOUS_SPECIFICATION, AgentAction.REQUEST_REVIEW)
                        elif state.quality_stop_reason == "quality_budget_reached":
                            finalize(TaskStatus.PARTIAL, StopReason.ROUND_LIMIT_REACHED)
                        elif state.quality_stop_reason == "quality_no_progress":
                            finalize(TaskStatus.PARTIAL, StopReason.NO_PROGRESS)
                        else:
                            state = state.updated(current_round=state.current_round + 1, quality_status="not_run", quality_stop_reason=None)
                            continue
                        break
                    if (state.config.quality_review_enabled and assessment.stop_reason == StopReason.NO_PROGRESS
                            and state.quality_assessments and state.quality_assessments[-1].decision.verdict == "request_revision"):
                        finalize(assessment.task_status, assessment.stop_reason, assessment.action,
                                 quality_stop_reason="quality_no_progress")
                        save_event("route_selected", node="test_quality_review", round=state.current_round,
                                   action="quality_no_progress", stop_reason="quality_no_progress")
                        break
                    finalize(assessment.task_status, assessment.stop_reason, assessment.action)
                    break
                # Optional trailing nodes extend the definition without changing the loop.
                save_event("workflow_stopped", round=state.current_round,
                           action=state.agent_state.next_action.value, stop_reason=state.stop_reason.value)
                for node in self.definition.nodes[2:]:
                    if node.agent_id != "test_quality_review":
                        invoke(node)
            state = state.updated(workflow_status="finished", current_node=None, finished_at=utc_now())
        except Exception as exc:
            # Exception messages may include credentials from injected clients.
            # Persist a controlled code and preserve every previously committed output.
            code = exc.code if isinstance(exc, NodeFailure) else "invalid_handoff" if isinstance(exc, (ValueError, TypeError)) else "node_exception"
            if state.config.quality_review_enabled and state.current_node == "test_quality_review":
                state = state.updated(quality_status="error", quality_stop_reason="review_error")
            if node_started is not None:
                save_event("node_failed", node=state.current_node, round=state.current_round,
                           duration_seconds=perf_counter() - node_started, error_code=code)
            if measured:
                finalize(TaskStatus.PARTIAL if state.selected_round else TaskStatus.FAILED,
                         StopReason.UNRECOVERABLE_ERROR)
            state = state.updated(workflow_status="error", error_code=code,
                                  stop_reason=StopReason.UNRECOVERABLE_ERROR, finished_at=utc_now())
            save_event("workflow_error", node=state.current_node,
                       round=state.current_round, error_code=code, stop_reason=state.stop_reason.value)
        if measured:
            from .reporting import export_result
            # Persistence/export failures propagate: never claim an unwritten report.
            try:
                export_result(state)
            except Exception:
                finalize(TaskStatus.PARTIAL if state.selected_round else TaskStatus.FAILED, StopReason.UNRECOVERABLE_ERROR)
                state = state.updated(workflow_status="error", error_code="export_error", finished_at=utc_now())
                save_event("workflow_error", round=state.current_round, error_code="export_error")
                raise
        if state.workflow_status == "finished":
            save_event("workflow_finished")
        return state


class NodeFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
