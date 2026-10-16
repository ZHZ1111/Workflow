"""Single-action AgentDev reuse. No execution tools or multi-round entry points."""

from dataclasses import dataclass
import json
from pathlib import Path

from testgen_agent.agent import build_action_context, choose_action
from testgen_agent.generation import (
    GenerationClient, InitialCandidateArtifacts, generate_initial_candidate, stage_revision_candidate,
)
from testgen_agent.llm_client import GenerationContext, LLMError, LLMGenerationResult
from testgen_agent.models import AgentAction, AgentState, CandidateRound, TokenUsage
from testgen_agent.workspace import round_directory

from ..contracts import AgentContext, ArtifactRef
from ..storage import atomic_write, file_digest, task_path, verify_artifact
from .quality_revision import build_quality_revision_context, validate_quality_revision


class GenerationAdapterError(ValueError):
    def __init__(self, code: str, category: str):
        super().__init__(code)
        self.code = code
        self.category = category


class RecordedClient:
    """Invoke the injected client once; its own bounded retry policy stays intact."""

    def __init__(self, client: GenerationClient, max_attempts: int):
        self.client = client
        self.max_attempts = max_attempts
        self.calls = 0
        self.result: LLMGenerationResult | None = None
        self.attempts: int | None = 0

    def generate_tests(self, context: GenerationContext) -> LLMGenerationResult:
        if self.calls:
            raise GenerationAdapterError("client_called_twice", "budget")
        self.calls += 1
        self.attempts = None  # Unknown until a structured response/failure reports it.
        try:
            result = self.client.generate_tests(context)
        except LLMError as error:
            self.attempts = error.attempts
            raise
        if not isinstance(result, LLMGenerationResult):
            raise GenerationAdapterError("invalid_client_result", "contract")
        self.result = LLMGenerationResult.model_validate_json(result.canonical_json())
        self.attempts = self.result.attempts
        if self.attempts > self.max_attempts:
            raise GenerationAdapterError("request_budget_exceeded", "budget")
        return self.result


def preflight(context: AgentContext) -> AgentState:
    state = context.state
    business = state.agent_state
    root = context.run_dir
    if context.agent_id != "test_generation" or state.workflow_status != "running":
        raise GenerationAdapterError("invalid_generation_context", "contract")
    if business.source_info is None or not (business.behavior_specification or "").strip():
        raise GenerationAdapterError("missing_source_or_specification", "configuration")
    if context.round != len(business.history) + 1 or business.current_round != len(business.history):
        raise GenerationAdapterError("invalid_generation_round", "budget")
    if context.round > state.config.max_rounds or business.stop_reason is not None:
        raise GenerationAdapterError("generation_budget_exhausted", "budget")
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise GenerationAdapterError("invalid_run_directory", "workspace")
    workspace = business.request.output_dir
    if workspace != root / "agent_workspace" or workspace.is_symlink() or workspace.is_junction():
        raise GenerationAdapterError("invalid_generation_workspace", "workspace")
    # SourceInfo is the snapshot analyzed by Workflow, never inferred by the model.
    if business.request.source_path.read_bytes() != business.source_info.source_code.encode(business.source_info.encoding):
        raise GenerationAdapterError("source_input_changed", "workspace")
    next_handoff = task_path(root, f"handoffs/{context.round:02d}/candidate.json", must_exist=False)
    if next_handoff.parent.exists():
        raise GenerationAdapterError("handoff_directory_exists", "workspace")
    if not business.history:
        if context.inbox:
            raise GenerationAdapterError("unexpected_initial_feedback", "contract")
        if workspace.exists():
            raise GenerationAdapterError("generation_workspace_exists", "workspace")
        return business

    if not workspace.is_dir():
        raise GenerationAdapterError("missing_generation_workspace", "workspace")
    subject = task_path(root, "agent_workspace/subject.py")
    if subject.read_bytes() != business.source_info.source_code.encode(business.source_info.encoding):
        raise GenerationAdapterError("source_snapshot_changed", "workspace")
    next_directory = task_path(root, f"agent_workspace/{round_directory(context.round).as_posix()}", must_exist=False)
    if next_directory.exists():
        raise GenerationAdapterError("candidate_round_exists", "workspace")
    # All previously staged generation artifacts retain their original digests.
    for reference in state.artifacts:
        if reference.kind in {"agentdev_candidate", "plan", "llm_result", "tests", "subject"}:
            verify_artifact(root, reference)
    for old in business.history:
        relative = f"agent_workspace/{round_directory(old.number).as_posix()}/candidate.json"
        saved = CandidateRound.model_validate_json(task_path(root, relative).read_text(encoding="utf-8"))
        if saved.test_path is not None and saved.test_path != round_directory(old.number) / "test_subject.py":
            raise GenerationAdapterError("invalid_historical_test_path", "workspace")
        expected_test = workspace / saved.test_path if saved.test_path is not None else None
        if (saved.generated, saved.validation_error, expected_test) != (old.generated, old.validation_error, old.test_path):
            raise GenerationAdapterError("candidate_history_changed", "workspace")
        if expected_test is not None and expected_test.read_text(encoding="utf-8") != old.generated.code:
            raise GenerationAdapterError("historical_test_changed", "workspace")

    if len(context.inbox) != 1:
        raise GenerationAdapterError("missing_revision_feedback", "contract")
    message = context.inbox[0]
    if message.type == "quality_feedback":
        try:
            validate_quality_revision(context)
        except (ValueError, OSError, StopIteration, SyntaxError):
            raise GenerationAdapterError("invalid_quality_feedback", "contract") from None
        return business
    if (message.type != "execution_feedback" or message.sender not in {"test_execution", "workflow"}
            or message.round != business.current_round or message not in state.messages
            or message.message_id in state.consumed_message_ids
            or message.payload.artifact not in state.artifacts):
        raise GenerationAdapterError("invalid_revision_feedback", "contract")
    verify_artifact(root, message.payload.artifact)
    action = message.payload.recommended_action
    if action not in {AgentAction.REPAIR_GENERATED_TEST, AgentAction.ADD_MISSING_CASES}:
        raise GenerationAdapterError("generation_action_not_allowed", "contract")
    decision = choose_action(business)
    if action != business.next_action or action != decision.action:
        raise GenerationAdapterError("generation_action_mismatch", "contract")
    return business


def revision_context(context: AgentContext, business: AgentState) -> GenerationContext:
    if context.inbox[0].type == "quality_feedback":
        return build_quality_revision_context(context)
    prompt = build_action_context(business)
    # Consume the actual verified handoff; its text is data, not host instructions.
    return prompt.updated(user_prompt=prompt.user_prompt + "\n<WORKFLOW_HANDOFF_DATA>\n"
                          + json.dumps(context.inbox[0].model_dump(mode="json"), ensure_ascii=False)
                          + "\n</WORKFLOW_HANDOFF_DATA>")


def generate_one(context: AgentContext, business: AgentState, client: RecordedClient) -> InitialCandidateArtifacts:
    if not business.history:
        return generate_initial_candidate(
            source=business.source_info, behavior_specification=business.behavior_specification,
            oracle_mode=business.oracle_mode, workspace=business.request.output_dir, client=client,
        )
    result = client.generate_tests(revision_context(context, business))
    return stage_revision_candidate(source=business.source_info, llm_result=result,
                                    workspace=business.request.output_dir, history=business.history,
                                    expected_oracle_mode=business.oracle_mode)


def cumulative_usage(business: AgentState, latest: TokenUsage | None) -> TokenUsage | None:
    previous = business.token_usage
    if not business.history:
        return latest
    if previous is None or latest is None:
        return None
    return TokenUsage(**{name: None if getattr(previous, name) is None or getattr(latest, name) is None
                        else getattr(previous, name) + getattr(latest, name)
                        for name in ("prompt_tokens", "completion_tokens", "total_tokens")})


@dataclass(frozen=True)
class WorkflowCandidate:
    candidate: CandidateRound
    artifacts: tuple[ArtifactRef, ...]
    handoff: ArtifactRef


def adapt_candidate(context: AgentContext, staged: InitialCandidateArtifacts) -> WorkflowCandidate:
    # Vendor JSON retains its relative inner test path. Workflow uses an absolute
    # CandidateRound test path as P1 does, with a separate wrapper handoff file.
    candidate = staged.candidate.updated(test_path=staged.test_path)
    handoff_path = task_path(context.run_dir, f"handoffs/{context.round:02d}/candidate.json", must_exist=False)
    handoff_path.parent.mkdir(parents=True, exist_ok=False)
    atomic_write(handoff_path, candidate.canonical_json(indent=2) + "\n")

    def reference(path: Path, kind: str) -> ArtifactRef:
        relative = path.relative_to(context.run_dir).as_posix()
        resolved = task_path(context.run_dir, relative)
        return ArtifactRef(path=relative, kind=kind, sha256=file_digest(resolved),
                           round=context.round, candidate_id=f"{context.run_id}:{context.round}")

    handoff = reference(handoff_path, "candidate")
    artifacts = [handoff, reference(staged.candidate_path, "agentdev_candidate"),
                 reference(staged.plan_path, "plan"), reference(staged.llm_result_path, "llm_result")]
    if context.round == 1:
        artifacts.append(reference(staged.subject_path, "subject"))
    if staged.test_path is not None:
        artifacts.append(reference(staged.test_path, "tests"))
    return WorkflowCandidate(candidate=candidate, artifacts=tuple(artifacts), handoff=handoff)
