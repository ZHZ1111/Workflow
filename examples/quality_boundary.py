"""折扣门槛边界样例；输入域与舍入规则见相邻规格。"""


def discounted_total(amount: int) -> int:
    if amount >= 100:
        return amount * 9 // 10
    return amount
