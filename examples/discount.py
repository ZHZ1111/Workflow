"""折扣计算样例；按 discount.md 的明确规格含一个有意保留的缺陷。"""


def calculate_discount(amount: int) -> int:
    """非负整数金额满 100 打九折，折后金额向下取整；未满则原价。

    bool 或非整数抛 TypeError，负整数抛 ValueError。
    """
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise TypeError("amount must be an integer, excluding bool")
    if amount < 0:
        raise ValueError("amount must be non-negative")
    if amount > 100:  # 有意的边界缺陷：规格要求 amount >= 100。
        return amount * 9 // 10
    return amount
