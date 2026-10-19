"""手写参考测试，用于验证 order_flow 示例与其行为规格一致。"""

from copy import deepcopy

import pytest

from order_flow import (
    apply_discount,
    calculate_shipping,
    calculate_subtotal,
    checkout,
    validate_item,
)


@pytest.mark.parametrize("price, quantity", [(0, 1), (5, 20), (1_000_000, 2)])
def test_validate_item_accepts_boundaries(price, quantity):
    assert validate_item(price, quantity) is None


@pytest.mark.parametrize("price, quantity", [(True, 1), (5, False), (5.0, 1), (5, "2")])
def test_validate_item_rejects_types(price, quantity):
    with pytest.raises(TypeError):
        validate_item(price, quantity)


@pytest.mark.parametrize("price, quantity", [(-1, 1), (5, 0), (5, 21)])
def test_validate_item_rejects_values(price, quantity):
    with pytest.raises(ValueError):
        validate_item(price, quantity)


def test_calculate_subtotal_and_input_immutability():
    items = [(20, 2), [15, 3], (0, 1)]
    original = deepcopy(items)
    assert calculate_subtotal(items) == 85
    assert items == original


@pytest.mark.parametrize("items, error", [([], ValueError), ([(5,)], ValueError),
                                          ([(5, 0)], ValueError), ((5, 2), TypeError)])
def test_calculate_subtotal_rejects_invalid_cart(items, error):
    with pytest.raises(error):
        calculate_subtotal(items)


@pytest.mark.parametrize("subtotal, member, coupon, expected", [
    (55, True, None, 49), (55, True, "SAVE10", 39),
    (55, False, "SAVE10", 45), (55, True, "FREESHIP", 49),
    (0, False, None, 0),
])
def test_apply_discount_order_and_rounding(subtotal, member, coupon, expected):
    assert apply_discount(subtotal, member, coupon) == expected


@pytest.mark.parametrize("subtotal, member, coupon, error", [
    (49, False, "SAVE10", ValueError), (-1, False, None, ValueError),
    (True, False, None, TypeError), (50, 1, None, TypeError),
    (50, False, "INVALID", ValueError),
])
def test_apply_discount_rejects_invalid_inputs(subtotal, member, coupon, error):
    with pytest.raises(error):
        apply_discount(subtotal, member, coupon)


@pytest.mark.parametrize("amount, destination, coupon, expected", [
    (99, "local", None, 12), (100, "local", None, 0),
    (40, "local", "FREESHIP", 0), (100, "remote", "FREESHIP", 25),
])
def test_calculate_shipping(amount, destination, coupon, expected):
    assert calculate_shipping(amount, destination, coupon) == expected


@pytest.mark.parametrize("amount, destination, coupon, error", [
    (True, "local", None, TypeError),
    (-1, "local", None, ValueError),
    (40, "unknown", None, ValueError),
    (40, "local", "INVALID", ValueError),
])
def test_calculate_shipping_rejects_invalid_inputs(amount, destination, coupon, error):
    with pytest.raises(error):
        calculate_shipping(amount, destination, coupon)


@pytest.mark.parametrize("items, options, expected", [
    ([(20, 2), [15, 3]], {}, (85, 85, 12, 97)),
    ([(50, 2)], {"member": True}, (100, 90, 12, 102)),
    ([(20, 2)], {"coupon": "FREESHIP"}, (40, 40, 0, 40)),
    ([(20, 2)], {"coupon": "FREESHIP", "destination": "remote"}, (40, 40, 25, 65)),
    ([(55, 1)], {"member": True, "coupon": "SAVE10"}, (55, 39, 12, 51)),
])
def test_checkout_real_collaboration(items, options, expected):
    original = deepcopy(items)
    result = checkout(items, **options)
    assert result == dict(zip(("subtotal", "discounted_subtotal", "shipping", "total"), expected))
    assert items == original


@pytest.mark.parametrize("items, options", [
    ([(10, 0)], {}),
    ([(10, 1)], {"coupon": "SAVE10"}),
    ([(10, 1)], {"destination": "unknown"}),
])
def test_checkout_propagates_validation_errors(items, options):
    with pytest.raises(ValueError):
        checkout(items, **options)
