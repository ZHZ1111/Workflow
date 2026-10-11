"""复杂测试样例：购物车、会员优惠、优惠券与配送结算，金额单位为分。"""


def item_total(unit_price: int, quantity: int) -> int:
    """校验商品单价及数量，返回该项原价。"""
    if type(unit_price) is not int or type(quantity) is not int:
        raise TypeError("price and quantity must be integers")
    if not 0 <= unit_price <= 1_000_000:
        raise ValueError("price out of range")
    if not 1 <= quantity <= 99:
        raise ValueError("quantity out of range")
    return unit_price * quantity


def cart_subtotal(items: list) -> int:
    """将 1–20 项 (单价, 数量) 相加，不修改购物车。"""
    if not isinstance(items, list):
        raise TypeError("cart must be a list")
    if not 1 <= len(items) <= 20:
        raise ValueError("cart must contain 1 to 20 items")
    subtotal = 0
    for item in items:
        if not isinstance(item, (tuple, list)):
            raise TypeError("item must be a pair")
        if len(item) != 2:
            raise ValueError("item must contain price and quantity")
        subtotal += item_total(item[0], item[1])
    return subtotal


def calculate_discount(subtotal: int, membership: str, coupon: str | None) -> int:
    """先会员折扣，再优惠券；资格看原价，总折扣封顶 3000 分。"""
    if type(subtotal) is not int:
        raise TypeError("subtotal must be an integer")
    if subtotal < 0:
        raise ValueError("subtotal must be nonnegative")
    if not isinstance(membership, str):
        raise TypeError("membership must be a string")
    if membership not in ("regular", "silver", "gold"):
        raise ValueError("unknown membership")
    if coupon is not None and not isinstance(coupon, str):
        raise TypeError("coupon must be a string or None")
    if coupon not in (None, "SAVE500", "PERCENT10"):
        raise ValueError("unknown coupon")
    rate = {"regular": 0, "silver": 5, "gold": 10}[membership]
    discount = subtotal * rate // 100
    if coupon == "SAVE500":
        if subtotal < 5000:
            raise ValueError("SAVE500 requires subtotal >= 5000")
        discount += 500
    elif coupon == "PERCENT10":
        if subtotal < 20000:
            raise ValueError("PERCENT10 requires subtotal >= 20000")
        discount += (subtotal - discount) // 10
    return min(discount, 3000)


def shipping_fee(payable: int, region: str, expedited: bool = False) -> int:
    """按优惠后货款和配送区域决定运费，加急始终另收 800 分。"""
    if type(payable) is not int:
        raise TypeError("payable must be an integer")
    if payable < 0:
        raise ValueError("payable must be nonnegative")
    if not isinstance(region, str):
        raise TypeError("region must be a string")
    if region not in ("city", "remote"):
        raise ValueError("unknown region")
    if type(expedited) is not bool:
        raise TypeError("expedited must be a boolean")
    threshold, base_fee = (15000, 1200) if region == "city" else (20000, 2500)
    fee = 0 if payable >= threshold else base_fee
    return fee + (800 if expedited else 0)


def checkout(items: list, membership: str = "regular", coupon: str | None = None,
             region: str = "city", expedited: bool = False) -> dict:
    """依次调用购物车、优惠、配送函数，返回完整结算明细。"""
    subtotal = cart_subtotal(items)
    discount = calculate_discount(subtotal, membership, coupon)
    shipping = shipping_fee(subtotal - discount, region, expedited)
    return {"subtotal": subtotal, "discount": discount, "shipping": shipping,
            "total": subtotal - discount + shipping}
