"""Two fake nodes use a real handoff, with simulated observation clearly labeled."""

from importlib.resources import files
from pathlib import Path
from typing import Literal

from test_workflow.agents.test_generation.core.generation import validate_generated_tests
from test_workflow.agents.test_generation.core.models import CandidateRound, ContractModel, GeneratedTests

from ..contracts import AgentContext, AgentResult, ArtifactRef, Message, MessagePayload, StatePatch
from ..storage import atomic_write, file_digest, task_path, verify_artifact


class DemoObservation(ContractModel):
    mode: Literal["demo"] = "demo"
    pytest_executed: Literal[False] = False
    model_called: Literal[False] = False
    consumed_message_id: str
    candidate_id: str
    candidate_sha256: str
    test_sha256: str


class FakeGenerationAgent:
    def __init__(self, fixture: Path | None = None):
        self.fixture = fixture

    def run(self, context: AgentContext) -> AgentResult:
        text = (self.fixture.read_text(encoding="utf-8") if self.fixture is not None else
                files("test_workflow.demo").joinpath("data/calculation_complete.json").read_text(encoding="utf-8"))
        generated = GeneratedTests.model_validate_json(text)
        validate_generated_tests(generated, context.state.agent_state.source_info)
        directory = context.run_dir / "demo_artifacts" / f"{context.round:02d}"
        directory.mkdir(parents=True, exist_ok=False)
        test_path = directory / "test_subject.py"
        atomic_write(test_path, generated.code)
        candidate = CandidateRound(number=context.round, generated=generated, test_path=test_path)
        candidate_path = directory / "candidate.json"
        atomic_write(candidate_path, candidate.canonical_json(indent=2) + "\n")
        candidate_id = f"{context.run_id}:{context.round}"

        def reference(path: Path, kind: str):
            return ArtifactRef(path=path.relative_to(context.run_dir).as_posix(), kind=kind,
                               sha256=file_digest(path), round=context.round, candidate_id=candidate_id)

        candidate_ref = reference(candidate_path, "candidate")
        test_ref = reference(test_path, "tests")
        message = Message(message_id="msg-candidate-01", run_id=context.run_id, round=context.round,
                          sender=context.agent_id, receiver="test_execution", type="candidate_ready",
                          payload=MessagePayload(candidate_id=candidate_id, artifact=candidate_ref,
                                                 reason="Offline fixture candidate; pytest has not run."))
        return AgentResult(run_id=context.run_id, round=context.round, agent_id=context.agent_id,
                           patch=StatePatch(candidate=candidate), messages=(message,),
                           artifacts=(candidate_ref, test_ref))


class FakeExecutionAgent:
    def run(self, context: AgentContext) -> AgentResult:
        if len(context.inbox) != 1 or context.inbox[0].type != "candidate_ready":
            raise ValueError("demo execution requires exactly one candidate_ready message")
        message = context.inbox[0]
        candidate_path = verify_artifact(context.run_dir, message.payload.artifact)
        candidate = CandidateRound.model_validate_json(candidate_path.read_text(encoding="utf-8"))
        saved = context.state.agent_state.history[-1]
        if candidate != saved or candidate.number != context.round:
            raise ValueError("handoff file differs from the generator's committed candidate")
        test_ref = next(item for item in context.state.artifacts if item.kind == "tests" and item.round == context.round)
        test_path = verify_artifact(context.run_dir, test_ref)
        if test_path != candidate.test_path or test_path.read_text(encoding="utf-8") != candidate.generated.code:
            raise ValueError("handoff test code differs from the committed candidate")
        observation = DemoObservation(consumed_message_id=message.message_id,
                                      candidate_id=message.payload.candidate_id,
                                      candidate_sha256=message.payload.artifact.sha256,
                                      test_sha256=test_ref.sha256)
        relative = f"demo_artifacts/{context.round:02d}/observation.json"
        path = task_path(context.run_dir, relative, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, observation.canonical_json(indent=2) + "\n")
        artifact = ArtifactRef(path=relative, kind="demo_observation", sha256=file_digest(path),
                               round=context.round, candidate_id=message.payload.candidate_id)
        return AgentResult(run_id=context.run_id, round=context.round, agent_id=context.agent_id,
                           artifacts=(artifact,))
