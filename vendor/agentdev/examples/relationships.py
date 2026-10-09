"""同文件函数关系样例：checkout 通过 calculate_total 完成计价。"""


def calculate_total(unit_price: int, quantity: int) -> int:
    """返回非负整数单价与数量的乘积。"""
    return unit_price * quantity


def checkout(unit_price: int, quantity: int) -> int:
    """结账入口：返回 calculate_total 计算的总价。"""
    return calculate_total(unit_price, quantity)
