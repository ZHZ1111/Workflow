"""P1 sequential coordinator: validate handoffs, then commit state proposals."""

from pathlib import Path
from uuid import uuid4

from testgen_agent.models import AgentState, CandidateRound, ModelInfo, OracleMode, StopReason, TaskInput
from testgen_agent.tools import analyze_source

from .config import WorkflowConfig
from .contracts import AgentContext, AgentResult, StatePatch
from .definitions import WorkflowDefinition
from .events import EventWriter, MessageWriter
from .registry import AgentRegistry
from .state import StateStore, WorkflowState, utc_now
from .storage import atomic_write, verify_artifact


class HandoffError(ValueError):
    """A node output cannot be committed to this workflow."""


def create_run(*, source: Path, specification: Path, output: Path,
               config: WorkflowConfig | None = None) -> WorkflowState:
    config = config or WorkflowConfig()
    source = source.resolve(strict=True)
    specification = specification.resolve(strict=True)
    # Validate inputs before creating a task or invoking any node.
    facts = analyze_source(source)
    spec = specification.read_text(encoding="utf-8")
    if not spec.strip():
        raise ValueError("a nonempty UTF-8 behavior specification is required")
    run_id = f"run-{uuid4().hex}"
    root = output.resolve() / run_id
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
        if (result.run_id, result.round, result.agent_id) != (
            state.run_id, state.current_round, state.current_node,
        ):
            raise HandoffError("result belongs to another task, round or node")
        if result.status != "ok":
            raise HandoffError("node returned an error result")
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
            if not destinations or destinations[0].receives_from != state.current_node:
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
        return state.updated(
            agent_state=business, artifacts=artifacts,
            messages=(*state.messages, *result.messages),
            consumed_message_ids=(*state.consumed_message_ids, *consumed),
        )

    def run(self, initial: WorkflowState) -> WorkflowState:
        if initial.workflow_status != "pending":
            raise ValueError("P1 starts new pending tasks; persisted tasks are read-only")
        if initial.config.mode != "demo":
            raise ValueError("P1 only supports offline handoff demonstrations")
        state = WorkflowState.model_validate_json(initial.canonical_json())
        store = StateStore(state.run_dir)
        events = EventWriter(state.run_dir, state.run_id)
        messages = MessageWriter(state.run_dir)

        def save_event(event_type, **fields):
            nonlocal state
            events.append(event_type, **fields)
            state = state.updated(last_event_seq=events.seq)
            store.save(state)

        try:
            state = state.updated(workflow_status="running", current_round=1)
            save_event("workflow_started")
            for node in self.definition.nodes:
                state = state.updated(current_node=node.agent_id)
                save_event("node_started", node=node.agent_id, round=state.current_round)
                inbox = tuple(message for message in state.messages
                              if message.receiver == node.agent_id
                              and message.message_id not in state.consumed_message_ids)
                if node.receives_from is not None and not inbox:
                    raise HandoffError("consumer did not receive its required handoff")
                for message in inbox:
                    if message.sender != node.receives_from or message.round != state.current_round:
                        raise HandoffError("consumer received a foreign sender or source round")
                    verify_artifact(state.run_dir, message.payload.artifact)
                context = AgentContext(run_id=state.run_id, round=state.current_round,
                                       agent_id=node.agent_id, run_dir=state.run_dir,
                                       snapshot_json=state.canonical_json(), inbox=inbox)
                result = self.registry.get(node.agent_id).run(context)
                proposed = self.apply_result(state, result, tuple(item.message_id for item in inbox))
                # Validate the whole proposal before writing its handoff journal.
                for message in result.messages:
                    messages.append(message)
                    events.append("message_sent", node=node.agent_id, round=message.round,
                                  message_id=message.message_id)
                state = proposed.updated(last_event_seq=events.seq)
                save_event("node_finished", node=node.agent_id, round=state.current_round)
            state = state.updated(workflow_status="finished", current_node=None, finished_at=utc_now())
            save_event("workflow_finished")
        except Exception as exc:
            # Exception messages may include credentials from injected clients.
            # Persist a controlled code and preserve every previously committed output.
            code = "invalid_handoff" if isinstance(exc, (ValueError, TypeError)) else "node_exception"
            state = state.updated(workflow_status="error", error_code=code,
                                  stop_reason=StopReason.UNRECOVERABLE_ERROR, finished_at=utc_now())
            save_event("workflow_error", node=state.current_node,
                       round=state.current_round, error_code=code)
        return state
