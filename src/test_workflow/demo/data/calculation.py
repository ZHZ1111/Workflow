"""正常计算样例：输入域见相邻 calculation.md。"""


def calculate_total(unit_price: int, quantity: int) -> int:
    """返回单价乘以数量；输入为非负整数，零单价或零数量返回 0。

    输入域是调用方前置条件，不包含 bool；域外行为未定义。
    """
    return unit_price * quantity
