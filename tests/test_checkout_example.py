"""Validate the complex example against its published business scenarios."""

import copy
import importlib.util
from pathlib import Path

import pytest
from test_workflow.agents.test_generation.core.tools import analyze_source

SOURCE = Path(__file__).resolve().parents[1] / "examples/checkout.py"
definition = importlib.util.spec_from_file_location("checkout_example", SOURCE)
example = importlib.util.module_from_spec(definition)
definition.loader.exec_module(example)


def test_example_is_supported_by_workflow():
    assert len(analyze_source(SOURCE).functions) == 5


@pytest.mark.parametrize("price,quantity,total", [(0, 1, 0), (1, 99, 99), (1000000, 99, 99000000)])
def test_item_boundaries(price, quantity, total):
    assert example.item_total(price, quantity) == total


@pytest.mark.parametrize("price,quantity,error", [
    (True, 1, TypeError), (1, False, TypeError), (1.0, 1, TypeError), (1, "1", TypeError),
    (-1, 1, ValueError), (1000001, 1, ValueError), (1, 0, ValueError), (1, 100, ValueError),
])
def test_invalid_items(price, quantity, error):
    with pytest.raises(error):
        example.item_total(price, quantity)


@pytest.mark.parametrize("items,error", [
    (None, TypeError), (((1, 1),), TypeError), ([], ValueError), ([(1, 1)] * 21, ValueError),
    ([{}], TypeError), ([(1,)], ValueError), ([[1, 1, 1]], ValueError), ([(1, 0)], ValueError),
])
def test_invalid_carts(items, error):
    with pytest.raises(error):
        example.cart_subtotal(items)


def test_cart_maximum_and_nested_input_preservation():
    assert example.cart_subtotal([(1000000, 99)] * 20) == 1980000000
    items = [(1000, 2), [500, 3]]
    before = copy.deepcopy(items)
    assert example.checkout(items, "silver") == {
        "subtotal": 3500, "discount": 175, "shipping": 1200, "total": 4525}
    assert example.checkout(items, "silver")["total"] == 4525
    assert items == before


@pytest.mark.parametrize("subtotal,member,coupon,discount", [
    (0, "regular", None, 0), (101, "silver", None, 5), (101, "gold", None, 10),
    (5000, "regular", "SAVE500", 500), (5000, "gold", "SAVE500", 1000),
    (20000, "regular", "PERCENT10", 2000), (20000, "silver", "PERCENT10", 2900),
    (20000, "gold", "PERCENT10", 3000), (30000, "gold", None, 3000), (40000, "gold", None, 3000),
])
def test_discount_order_rounding_and_cap(subtotal, member, coupon, discount):
    assert example.calculate_discount(subtotal, member, coupon) == discount


@pytest.mark.parametrize("subtotal,member,coupon,error", [
    (True, "regular", None, TypeError), (-1, "regular", None, ValueError),
    (100, None, None, TypeError), (100, "Gold", None, ValueError),
    (100, "regular", 0, TypeError), (100, "regular", "", ValueError),
    (4999, "regular", "SAVE500", ValueError), (19999, "gold", "PERCENT10", ValueError),
])
def test_discount_invalid_inputs(subtotal, member, coupon, error):
    with pytest.raises(error):
        example.calculate_discount(subtotal, member, coupon)


@pytest.mark.parametrize("payable,region,base", [
    (0, "city", 1200), (14999, "city", 1200), (15000, "city", 0),
    (19999, "remote", 2500), (20000, "remote", 0),
])
@pytest.mark.parametrize("expedited", [False, True])
def test_shipping_thresholds_and_express_surcharge(payable, region, base, expedited):
    assert example.shipping_fee(payable, region, expedited) == base + (800 if expedited else 0)


@pytest.mark.parametrize("payable,region,expedited,error", [
    (False, "city", False, TypeError), (-1, "city", False, ValueError),
    (0, None, False, TypeError), (0, "CITY", False, ValueError), (0, "city", 1, TypeError),
])
def test_shipping_invalid_inputs(payable, region, expedited, error):
    with pytest.raises(error):
        example.shipping_fee(payable, region, expedited)


@pytest.mark.parametrize("items,member,coupon,region,express,amounts", [
    ([(2000, 3)], "regular", None, "city", False, (6000, 0, 1200, 7200)),
    ([(2000, 3)], "gold", "SAVE500", "city", False, (6000, 1100, 1200, 6100)),
    ([(15000, 1)], "regular", None, "city", False, (15000, 0, 0, 15000)),
    ([(15000, 1)], "silver", None, "city", False, (15000, 750, 1200, 15450)),
    ([(20000, 1)], "silver", "PERCENT10", "city", False, (20000, 2900, 0, 17100)),
    ([(20000, 1)], "gold", "PERCENT10", "city", True, (20000, 3000, 800, 17800)),
    ([(20000, 1)], "gold", None, "remote", False, (20000, 2000, 2500, 20500)),
    ([(40000, 1)], "silver", "PERCENT10", "remote", True, (40000, 3000, 800, 37800)),
    ([(0, 1)], "regular", None, "city", False, (0, 0, 1200, 1200)),
])
def test_checkout_real_collaboration(items, member, coupon, region, express, amounts):
    assert example.checkout(items, member, coupon, region, express) == dict(
        zip(("subtotal", "discount", "shipping", "total"), amounts))


@pytest.mark.parametrize("kwargs,error", [
    ({"items": []}, ValueError), ({"items": [(1, 1)], "membership": "unknown"}, ValueError),
    ({"items": [(1, 1)], "region": "unknown"}, ValueError),
])
def test_checkout_propagates_stage_errors(kwargs, error):
    with pytest.raises(error):
        example.checkout(**kwargs)
