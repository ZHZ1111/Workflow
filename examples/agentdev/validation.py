"""多分支校验样例：成绩分类及异常输入。"""


def classify_score(score: int) -> str:
    """将 0..100 的整数成绩分类，bool 不属于合法整数输入。

    0..59 返回 fail，60..89 返回 pass，90..100 返回 excellent。
    非整数或 bool 抛 TypeError；整数超出范围抛 ValueError。
    """
    if isinstance(score, bool) or not isinstance(score, int):
        raise TypeError("score must be an integer, excluding bool")
    if score < 0 or score > 100:
        raise ValueError("score must be between 0 and 100")
    if score < 60:
        return "fail"
    if score < 90:
        return "pass"
    return "excellent"
