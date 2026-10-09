"""订单结算样例：校验、折扣、优惠券和运费的多函数协作。"""


def validate_item(unit_price: int, quantity: int) -> None:
    """校验商品单价和购买数量；成功时不返回值。"""
    if isinstance(unit_price, bool) or not isinstance(unit_price, int):
        raise TypeError("unit_price must be an integer, excluding bool")
    if unit_price < 0:
        raise ValueError("unit_price must be non-negative")
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise TypeError("quantity must be an integer, excluding bool")
    if not 1 <= quantity <= 20:
        raise ValueError("quantity must be between 1 and 20")


def calculate_subtotal(items: list[tuple[int, int] | list[int]]) -> int:
    """汇总非空购物车，每项为 (单价, 数量)。"""
    if not isinstance(items, list):
        raise TypeError("items must be a list")
    if not items:
        raise ValueError("items must not be empty")
    subtotal = 0
    for item in items:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError("each item must contain price and quantity")
        unit_price, quantity = item
        validate_item(unit_price, quantity)
        subtotal += unit_price * quantity
    return subtotal


def apply_discount(subtotal: int, member: bool, coupon: str | None) -> int:
    """会员先打九折；SAVE10 再减 10 元；FREESHIP 不改变商品金额。"""
    if isinstance(subtotal, bool) or not isinstance(subtotal, int):
        raise TypeError("subtotal must be an integer, excluding bool")
    if subtotal < 0:
        raise ValueError("subtotal must be non-negative")
    if not isinstance(member, bool):
        raise TypeError("member must be a bool")
    if coupon not in (None, "SAVE10", "FREESHIP"):
        raise ValueError("unknown coupon")
    if coupon == "SAVE10" and subtotal < 50:
        raise ValueError("SAVE10 requires a subtotal of at least 50")

    discounted = subtotal * 9 // 10 if member else subtotal
    if coupon == "SAVE10":
        discounted = max(0, discounted - 10)
    return discounted


def calculate_shipping(discounted_subtotal: int, destination: str, coupon: str | None) -> int:
    """本地订单满 100 元或使用 FREESHIP 免运费；偏远地区固定 25 元。"""
    if isinstance(discounted_subtotal, bool) or not isinstance(discounted_subtotal, int):
        raise TypeError("discounted_subtotal must be an integer, excluding bool")
    if discounted_subtotal < 0:
        raise ValueError("discounted_subtotal must be non-negative")
    if destination not in ("local", "remote"):
        raise ValueError("unknown destination")
    if coupon not in (None, "SAVE10", "FREESHIP"):
        raise ValueError("unknown coupon")
    if destination == "remote":
        return 25
    if discounted_subtotal >= 100 or coupon == "FREESHIP":
        return 0
    return 12


def checkout(
    items: list[tuple[int, int] | list[int]],
    member: bool = False,
    coupon: str | None = None,
    destination: str = "local",
) -> dict[str, int]:
    """按商品校验、小计、折扣、运费的顺序结算，不修改购物车。"""
    subtotal = calculate_subtotal(items)
    discounted_subtotal = apply_discount(subtotal, member, coupon)
    shipping = calculate_shipping(discounted_subtotal, destination, coupon)
    return {
        "subtotal": subtotal,
        "discounted_subtotal": discounted_subtotal,
        "shipping": shipping,
        "total": discounted_subtotal + shipping,
    }
