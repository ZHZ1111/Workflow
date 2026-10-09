"""C1a extracts conservative call facts without inventing relationships."""

from pathlib import Path

import pytest

from testgen_agent import models as m
from testgen_agent.tools import (
    analyze_source,
    build_call_graph,
    candidate_entry_functions,
    find_call_cycles,
    find_explicit_data_flows,
)


def analyze(tmp_path: Path, source: str):
    path = tmp_path / "subject.py"
    path.write_text(source, encoding="utf-8")
    return analyze_source(path)


def edge_pairs(source):
    return [(item.caller, item.callee) for item in source.direct_calls]


def unresolved_expressions(source):
    sites = {item.call_id: item.expression for item in source.call_sites}
    return [sites[item.call_site_id] for item in source.unresolved_calls]


def test_relationship_example_produces_confirmed_edge_and_candidate_entry():
    root = Path(__file__).parents[1]

    source = analyze_source(root / "examples" / "relationships.py")

    site = source.call_sites[0]
    assert site.call_id == "call:checkout:11:11:11:48"
    assert site.caller == "checkout"
    assert site.expression == "calculate_total(unit_price, quantity)"
    assert site.source == "return calculate_total(unit_price, quantity)"
    assert site.argument_expressions == ("unit_price", "quantity")
    assert edge_pairs(source) == [("checkout", "calculate_total")]
    assert source.unresolved_calls == ()
    assert build_call_graph(source) == {
        "calculate_total": (),
        "checkout": ("calculate_total",),
    }
    assert candidate_entry_functions(source) == ("checkout",)


def test_nested_calls_preserve_explicit_data_flow_and_keyword_arguments(tmp_path):
    source = analyze(
        tmp_path,
        """def produce(value):
    return value + 1

def consume(value, label=None, **options):
    return value

def pipeline(value, options):
    return consume(produce(value), label=str(value), **options)
""",
    )

    assert [item.expression for item in source.call_sites] == [
        "consume(produce(value), label=str(value), **options)",
        "produce(value)",
        "str(value)",
    ]
    assert source.call_sites[0].argument_expressions == (
        "produce(value)", "label=str(value)", "**options",
    )
    assert edge_pairs(source) == [("pipeline", "consume"), ("pipeline", "produce")]
    assert source.unresolved_calls == ()
    assert candidate_entry_functions(source) == ("pipeline",)
    flows = find_explicit_data_flows(source)
    assert [(item.kind, item.via) for item in flows] == [("nested_call", "produce(value)")]


def test_simple_assignment_flow_remains_visible_in_call_sources(tmp_path):
    source = analyze(
        tmp_path,
        """def produce(value):
    return value + 1

def consume(value):
    return value * 2

def pipeline(value):
    intermediate = produce(value)
    return consume(intermediate)
""",
    )

    assert [(item.expression, item.source, item.argument_expressions) for item in source.call_sites] == [
        ("produce(value)", "intermediate = produce(value)", ("value",)),
        ("consume(intermediate)", "return consume(intermediate)", ("intermediate",)),
    ]
    assert edge_pairs(source) == [("pipeline", "produce"), ("pipeline", "consume")]
    flows = find_explicit_data_flows(source)
    assert [(item.kind, item.via) for item in flows] == [("simple_assignment", "intermediate")]


def test_builtin_and_standard_library_calls_are_observed_but_not_relationships(tmp_path):
    source = analyze(
        tmp_path,
        """import math as maths
from collections import deque

def measure(values):
    queue = deque(values)
    return maths.sqrt(len(queue))
""",
    )

    assert [item.expression for item in source.call_sites] == [
        "deque(values)", "maths.sqrt(len(queue))", "len(queue)",
    ]
    assert source.direct_calls == ()
    assert source.unresolved_calls == ()


def test_dynamic_calls_are_retained_as_unresolved_evidence(tmp_path):
    source = analyze(
        tmp_path,
        """def target(value):
    return value

def caller(callback, obj, registry, key):
    callback()
    obj.run()
    registry[key]()
    return unknown_factory()()
""",
    )

    assert source.direct_calls == ()
    assert unresolved_expressions(source) == [
        "callback()", "obj.run()", "registry[key]()", "unknown_factory()()", "unknown_factory()",
    ]
    assert all("无法静态确认" in item.reason for item in source.unresolved_calls)


@pytest.mark.parametrize(
    "caller_body",
    [
        "def caller(target):\n    return target(1)\n",
        "def caller():\n    target = lambda value: value\n    return target(1)\n",
        "def caller(values):\n    for target in values:\n        return target(1)\n",
        "def caller():\n    import math as target\n    return target(1)\n",
        "def caller():\n    def target(value):\n        return value\n    return target(1)\n",
        "def caller(value):\n    match value:\n        case target:\n            return target(1)\n",
    ],
)
def test_parameter_and_local_bindings_block_false_direct_edges(tmp_path, caller_body):
    source = analyze(tmp_path, "def target(value):\n    return value\n\n" + caller_body)

    assert source.direct_calls == ()
    assert unresolved_expressions(source) == ["target(1)"]
    assert "遮蔽" in source.unresolved_calls[0].reason


def test_assignment_anywhere_in_function_makes_name_local_before_call(tmp_path):
    source = analyze(
        tmp_path,
        """def target(value):
    return value

def caller(value):
    result = target(value)
    target = lambda item: item
    return result
""",
    )

    assert source.direct_calls == ()
    assert unresolved_expressions(source) == ["target(value)"]


@pytest.mark.parametrize(
    "source_text",
    [
        """def target(value):
    return value

original = target
target = original

def caller(value):
    return target(value)
""",
        """def identity(function):
    return function

@identity
def target(value):
    return value

def caller(value):
    return target(value)
""",
        """def target(value):
    return value

def replace():
    global target
    target = lambda value: value

def caller(value):
    return target(value)
""",
    ],
)
def test_module_rebinding_decorators_and_global_writes_make_edges_uncertain(tmp_path, source_text):
    source = analyze(tmp_path, source_text)

    assert ("caller", "target") not in edge_pairs(source)
    matching = [
        item for item in source.unresolved_calls
        if source.call_sites[[site.call_id for site in source.call_sites].index(item.call_site_id)].caller == "caller"
    ]
    assert len(matching) == 1
    assert "重新绑定" in matching[0].reason


def test_comprehension_target_shadowing_is_scope_aware(tmp_path):
    source = analyze(
        tmp_path,
        """def target(value):
    return value

def safe(values):
    return [target(value) for value in target(values)]

def shadowed(callbacks, values):
    return [target(value) for target in callbacks for value in target(values)]
""",
    )

    assert edge_pairs(source) == [("safe", "target"), ("safe", "target")]
    assert unresolved_expressions(source) == ["target(values)", "target(value)"]
    assert all("推导式" in item.reason for item in source.unresolved_calls)


def test_calls_in_nested_definitions_and_lambdas_are_not_attributed_to_outer(tmp_path):
    source = analyze(
        tmp_path,
        """def target(value):
    return value

def outer(value):
    def nested():
        return target(value)
    deferred = lambda: target(value)
    return value
""",
    )

    assert source.call_sites == ()
    assert source.direct_calls == ()


def test_direct_self_recursion_is_a_cycle_but_remains_an_entry_candidate(tmp_path):
    source = analyze(
        tmp_path,
        """def factorial(value):
    if value <= 1:
        return 1
    return value * factorial(value - 1)
""",
    )

    assert build_call_graph(source) == {"factorial": ("factorial",)}
    assert find_call_cycles(source) == (("factorial",),)
    assert candidate_entry_functions(source) == ("factorial",)


def test_mutual_recursion_uses_root_components_without_infinite_expansion(tmp_path):
    source = analyze(
        tmp_path,
        """def even(value):
    return value == 0 or odd(value - 1)

def odd(value):
    return value != 0 and even(value - 1)

def check(value):
    return even(value)
""",
    )

    assert build_call_graph(source) == {
        "even": ("odd",), "odd": ("even",), "check": ("even",),
    }
    assert find_call_cycles(source) == (("even", "odd"),)
    assert candidate_entry_functions(source) == ("check",)


def test_independent_functions_are_all_graph_root_candidates(tmp_path):
    source = analyze(
        tmp_path,
        """def alpha(value):
    return value + 1

def beta(value):
    return value * 2
""",
    )

    assert build_call_graph(source) == {"alpha": (), "beta": ()}
    assert candidate_entry_functions(source) == ("alpha", "beta")
    assert find_call_cycles(source) == ()


def test_nested_callable_spans_produce_unique_stable_call_ids(tmp_path):
    source = analyze(
        tmp_path,
        """def factory():
    return lambda: 1

def caller():
    return factory()()
""",
    )

    assert len({item.call_id for item in source.call_sites}) == 2
    assert edge_pairs(source) == [("caller", "factory")]
    assert unresolved_expressions(source) == ["factory()()"]


def test_graph_helpers_require_source_info():
    for helper in (
        build_call_graph, candidate_entry_functions, find_call_cycles, find_explicit_data_flows,
    ):
        with pytest.raises(TypeError, match="SourceInfo"):
            helper(object())


def test_agent_relationship_can_reference_real_analyzer_evidence():
    root = Path(__file__).parents[1]
    source = analyze_source(root / "examples" / "relationships.py")
    call_id = source.direct_calls[0].call_site_id
    plan = m.TestPlan(
        schema_version="1.1",
        target_functions=("calculate_total", "checkout"),
        oracle_mode="specification",
        entry_functions=("checkout",),
        relationships=(m.RelationshipAnalysis(
            relation_id="rel-checkout-total",
            kind="direct_call",
            involved_functions=("checkout", "calculate_total"),
            evidence=(m.RelationshipEvidence(
                kind="source",
                detail="checkout directly invokes calculate_total",
                line=11,
                call_site_ids=(call_id,),
            ),),
            confidence="confirmed",
        ),),
        strategy=(
            m.StrategyDecision(
                subject_kind="function", function_name="calculate_total",
                decision="unit_test", reason="focused calculation boundary",
            ),
            m.StrategyDecision(
                subject_kind="function", function_name="checkout",
                decision="unit_test", reason="public entry behavior",
            ),
            m.StrategyDecision(
                subject_kind="relationship", relationship_id="rel-checkout-total",
                decision="not_selected", reason="the direct edge is real but adds no separate oracle here",
            ),
        ),
    )

    m.validate_plan_against_source(plan, source)
