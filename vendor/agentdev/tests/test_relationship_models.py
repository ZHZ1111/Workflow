"""B1 schema 1.1 contracts for function evidence and autonomous test strategy."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from testgen_agent import models as m
from testgen_agent.config import load_config


@pytest.fixture
def source() -> m.SourceInfo:
    code = (
        "def calculate_total(unit_price, quantity):\n"
        "    return unit_price * quantity\n"
        "\n"
        "def checkout(unit_price, quantity):\n"
        "    return calculate_total(unit_price, quantity)\n"
    )
    return m.SourceInfo(
        source_path=Path("workflow.py"),
        source_code=code,
        functions=(
            m.FunctionInfo(
                name="calculate_total", signature="(unit_price, quantity)",
                start_line=1, end_line=2,
            ),
            m.FunctionInfo(
                name="checkout", signature="(unit_price, quantity)",
                start_line=4, end_line=5,
            ),
        ),
        call_sites=(m.CallSite(
            call_id="call-checkout-total",
            caller="checkout",
            expression="calculate_total(unit_price, quantity)",
            line=5,
            source="return calculate_total(unit_price, quantity)",
            argument_expressions=("unit_price", "quantity"),
        ),),
        direct_calls=(m.DirectCall(
            call_site_id="call-checkout-total",
            caller="checkout",
            callee="calculate_total",
        ),),
    )


@pytest.fixture
def plan() -> m.TestPlan:
    relationship = m.RelationshipAnalysis(
        relation_id="rel-checkout-total",
        kind="direct_call",
        involved_functions=("checkout", "calculate_total"),
        evidence=(m.RelationshipEvidence(
            kind="source",
            detail="checkout directly calls calculate_total",
            line=5,
            call_site_ids=("call-checkout-total",),
        ),),
        confidence="confirmed",
    )
    return m.TestPlan(
        schema_version="1.1",
        target_functions=("calculate_total", "checkout"),
        oracle_mode="specification",
        entry_functions=("checkout",),
        relationships=(relationship,),
        strategy=(
            m.StrategyDecision(
                subject_kind="function", function_name="calculate_total",
                decision="unit_test", reason="calculation has focused boundaries",
            ),
            m.StrategyDecision(
                subject_kind="function", function_name="checkout",
                decision="unit_test", reason="entry validation is independently observable",
            ),
            m.StrategyDecision(
                subject_kind="relationship", relationship_id="rel-checkout-total",
                decision="collaboration_test", reason="entry combines pricing behavior",
            ),
        ),
        scenarios=(
            m.TestScenario(
                scenario_id="unit-total",
                target_function="calculate_total",
                category="boundary",
                inputs=m.ScenarioInput(args=(0, 3)),
                expected_behavior="returns zero",
                expected_basis=m.ExpectedBasis(kind="specification", detail="zero price rule"),
                test_kind="unit",
                involved_functions=("calculate_total",),
            ),
            m.TestScenario(
                scenario_id="collaboration-checkout",
                target_function="checkout",
                category="normal",
                inputs=m.ScenarioInput(args=(12, 3)),
                expected_behavior="returns 36 through the pricing workflow",
                expected_basis=m.ExpectedBasis(kind="specification", detail="checkout total rule"),
                test_kind="collaboration",
                involved_functions=("checkout", "calculate_total"),
                relationship_ids=("rel-checkout-total",),
            ),
        ),
    )


@pytest.fixture
def generated(plan) -> m.GeneratedTests:
    return m.GeneratedTests(
        plan=plan,
        code=(
            "from subject import calculate_total, checkout\n\n"
            "def test_unit_total():\n    assert calculate_total(0, 3) == 0\n\n"
            "def test_collaboration_checkout():\n    assert checkout(12, 3) == 36\n"
        ),
        mappings=(
            m.TestCaseMapping(test_function="test_unit_total", scenario_ids=("unit-total",)),
            m.TestCaseMapping(
                test_function="test_collaboration_checkout",
                scenario_ids=("collaboration-checkout",),
            ),
        ),
    )


def test_source_call_evidence_and_plan_11_round_trip(source, plan):
    assert m.SourceInfo.model_validate_json(source.canonical_json()) == source
    assert m.TestPlan.model_validate_json(plan.canonical_json()) == plan
    assert source.direct_calls[0].callee == "calculate_total"
    assert plan.scenarios[1].test_kind == "collaboration"


@pytest.mark.parametrize("changes", [
    {"direct_calls": ({"call_site_id": "missing", "caller": "checkout", "callee": "calculate_total"},)},
    {"direct_calls": ({"call_site_id": "call-checkout-total", "caller": "calculate_total", "callee": "checkout"},)},
    {"direct_calls": ({"call_site_id": "call-checkout-total", "caller": "checkout", "callee": "unknown"},)},
    {"unresolved_calls": ({"call_site_id": "missing", "caller": "checkout", "reason": "dynamic"},)},
])
def test_source_rejects_invalid_call_classification(source, changes):
    with pytest.raises(ValidationError):
        source.updated(**changes)


def test_source_rejects_call_that_is_confirmed_and_unresolved(source):
    with pytest.raises(ValidationError):
        source.updated(unresolved_calls=(m.UnresolvedCall(
            call_site_id="call-checkout-total", caller="checkout", reason="shadowed name",
        ),))


@pytest.mark.parametrize("changes", [
    {"confidence": "unknown", "unresolved_questions": ()},
    {"kind": "unknown", "confidence": "candidate", "unresolved_questions": ("Which target?",)},
    {"involved_functions": ("checkout",)},
])
def test_relationship_confidence_and_function_count_are_consistent(plan, changes):
    with pytest.raises(ValidationError):
        plan.relationships[0].updated(**changes)


def test_unknown_relationship_retains_question():
    relationship = m.RelationshipAnalysis(
        relation_id="rel-dynamic",
        kind="unknown",
        involved_functions=("checkout",),
        evidence=(m.RelationshipEvidence(kind="source", detail="callback expression", line=5),),
        confidence="unknown",
        unresolved_questions=("Which function is bound at runtime?",),
    )
    assert relationship.confidence == "unknown" and relationship.unresolved_questions


@pytest.mark.parametrize("changes", [
    {"entry_functions": ()},
    {"strategy": ()},
    {"strategy": None},
    {"relationships": ()},
])
def test_schema_11_requires_complete_entries_relationship_strategy(plan, changes):
    if changes.get("strategy", object()) is None:
        changes = {"strategy": plan.strategy[:-1]}
    with pytest.raises(ValidationError):
        plan.updated(**changes)


def test_strategy_decisions_are_scoped_and_reasoned():
    with pytest.raises(ValidationError):
        m.StrategyDecision(
            subject_kind="function", function_name="checkout",
            decision="collaboration_test", reason="wrong scope",
        )
    with pytest.raises(ValidationError):
        m.StrategyDecision(
            subject_kind="relationship", relationship_id="rel",
            decision="unit_test", reason="wrong scope",
        )


@pytest.mark.parametrize("scenario_changes", [
    {"involved_functions": ("checkout",)},
    {"relationship_ids": ("missing",)},
    {"test_kind": "unit", "involved_functions": ("checkout", "calculate_total")},
    {"test_kind": "collaboration", "relationship_ids": ()},
    {"involved_functions": ("checkout",), "relationship_ids": ("rel-checkout-total",)},
])
def test_scenario_scope_and_relationship_references_are_validated(plan, scenario_changes):
    changed = plan.scenarios[1].updated(**scenario_changes)
    with pytest.raises(ValidationError):
        plan.updated(scenarios=(plan.scenarios[0], changed))


def test_data_flow_steps_reference_only_prior_results(plan):
    relationship = plan.relationships[0].updated(kind="data_flow", confidence="candidate")
    steps = (
        m.ScenarioStep(
            step_id="subtotal",
            function="calculate_total",
            args=(
                m.StepValue(kind="literal", value=12),
                m.StepValue(kind="literal", value=3),
            ),
        ),
        m.ScenarioStep(
            step_id="checkout-result",
            function="checkout",
            args=(m.StepValue(kind="step_result", step_id="subtotal"),),
        ),
    )
    scenario = plan.scenarios[1].updated(steps=steps)
    revised = plan.updated(relationships=(relationship,), scenarios=(plan.scenarios[0], scenario))
    assert revised.scenarios[1].steps[1].args[0].step_id == "subtotal"
    # Cross-step ordering is checked by TestScenario, not by one isolated step.
    with pytest.raises(ValidationError):
        scenario.updated(steps=(steps[1], steps[0]))


def test_legacy_10_plan_remains_readable():
    legacy = m.TestPlan.model_validate({
        "target_functions": ["calculate_total"],
        "oracle_mode": "specification",
        "scenarios": [{
            "scenario_id": "normal",
            "target_function": "calculate_total",
            "category": "normal",
            "inputs": {"args": [2, 3], "kwargs": {}},
            "expected_behavior": "returns 6",
            "expected_basis": {"kind": "specification", "detail": "multiplication rule"},
        }],
    })
    assert legacy.schema_version == "1.0"
    assert not legacy.relationships and not legacy.scenarios[0].involved_functions


def test_state_and_report_validate_relationship_evidence(source, generated, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(environ={"LLM_MODEL": "test-model", "DEEPSEEK_API_KEY": "relation-secret"})
    request = m.TaskInput(source_path=source.source_path, model=m.ModelInfo.from_config(config))
    candidate = m.CandidateRound(number=1, generated=generated)
    state = m.AgentState(
        schema_version="1.1",
        run_id="relationships",
        request=request,
        oracle_mode="specification",
        source_info=source,
        current_round=1,
        history=(candidate,),
    )
    report = m.FinalReport(
        schema_version="1.1",
        run_id="relationships",
        request=request,
        oracle_mode="specification",
        status="failed",
        stop_reason="unrecoverable_error",
        source_info=source,
        rounds=(candidate,),
    )
    assert state.history[0].generated.plan.relationships == report.rounds[0].generated.plan.relationships


def test_unresolved_tool_call_cannot_be_upgraded_to_confirmed(source, generated, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(environ={"LLM_MODEL": "test-model", "DEEPSEEK_API_KEY": "relation-secret"})
    request = m.TaskInput(source_path=source.source_path, model=m.ModelInfo.from_config(config))
    unresolved_source = source.updated(
        direct_calls=(),
        unresolved_calls=(m.UnresolvedCall(
            call_site_id="call-checkout-total", caller="checkout", reason="local name is reassigned",
        ),),
    )
    with pytest.raises(ValidationError, match="unresolved source calls"):
        m.AgentState(
            schema_version="1.1",
            run_id="relationships",
            request=request,
            oracle_mode="specification",
            source_info=unresolved_source,
            current_round=1,
            history=(m.CandidateRound(number=1, generated=generated),),
        )


def test_confirmed_direct_relationship_preserves_call_direction(source, generated, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(environ={"LLM_MODEL": "test-model", "DEEPSEEK_API_KEY": "relation-secret"})
    request = m.TaskInput(source_path=source.source_path, model=m.ModelInfo.from_config(config))
    relationship = generated.plan.relationships[0].updated(
        involved_functions=("calculate_total", "checkout"),
    )
    scenario = generated.plan.scenarios[1].updated(
        involved_functions=("calculate_total", "checkout"),
    )
    reversed_plan = generated.plan.updated(
        relationships=(relationship,),
        scenarios=(generated.plan.scenarios[0], scenario),
    )
    with pytest.raises(ValidationError, match="tool-confirmed call evidence"):
        m.AgentState(
            schema_version="1.1",
            run_id="relationships",
            request=request,
            oracle_mode="specification",
            source_info=source,
            current_round=1,
            history=(m.CandidateRound(
                number=1, generated=generated.updated(plan=reversed_plan),
            ),),
        )


def test_strategy_selection_must_match_scenarios(plan):
    decisions = tuple(
        item.updated(decision="not_selected")
        if item.subject_kind == "function" and item.function_name == "calculate_total"
        else item
        for item in plan.strategy
    )
    with pytest.raises(ValidationError, match="unit-test strategy"):
        plan.updated(strategy=decisions)

    unknown = plan.relationships[0].updated(
        kind="unknown",
        confidence="unknown",
        involved_functions=("checkout",),
        unresolved_questions=("Which callable is used?",),
    )
    decisions = tuple(
        item.updated(decision="not_selected")
        if item.subject_kind == "relationship"
        else item
        for item in plan.strategy
    )
    without_collaboration = plan.updated(
        relationships=(unknown,),
        strategy=decisions,
        scenarios=(plan.scenarios[0],),
    )
    assert without_collaboration.relationships[0].confidence == "unknown"
    with pytest.raises(ValidationError, match="unknown relationships"):
        without_collaboration.updated(strategy=plan.strategy, scenarios=(plan.scenarios[0],))


def test_schema_versions_and_report_source_are_required_together(source, generated, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(environ={"LLM_MODEL": "test-model", "DEEPSEEK_API_KEY": "relation-secret"})
    request = m.TaskInput(source_path=source.source_path, model=m.ModelInfo.from_config(config))
    candidate = m.CandidateRound(number=1, generated=generated)
    with pytest.raises(ValidationError, match="schema versions"):
        m.AgentState(
            run_id="relationships", request=request, oracle_mode="specification",
            source_info=source, current_round=1, history=(candidate,),
        )
    with pytest.raises(ValidationError, match="source metadata"):
        m.FinalReport(
            schema_version="1.1",
            run_id="relationships",
            request=request,
            oracle_mode="specification",
            status="failed",
            stop_reason="unrecoverable_error",
            rounds=(candidate,),
        )
