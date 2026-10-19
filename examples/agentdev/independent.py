"""两个彼此独立的纯函数样例。"""


def rectangle_area(width: int, height: int) -> int:
    """返回非负整数宽高的矩形面积。"""
    return width * height


def is_even(value: int) -> bool:
    """返回整数是否为偶数。"""
    return value % 2 == 0
