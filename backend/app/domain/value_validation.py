"""监测浓度数值的合理域校验 (服务端最终判定, 所有写入入口共用).

规则按因子区分, 与前端的前置提示共用同一份定义 (见 standards.POLLUTANTS):

1. 必须是有限数字 (拒绝 NaN / Infinity / 非数字字符串);
2. 浓度不能为负数;
3. 不能超过该因子的物理合理量程上限 value_max (明显不合理的极大值)。

超标与异常的区别: 超标是“真实但超过 GB 3095-2012 限值”, 参与达标率统计;
异常值是“物理上不成立的录入错误”, 不得入库, 历史遗留的异常值在统计中单独说明。
"""
import math

from sqlalchemy import case, or_

from .standards import POLLUTANT_CODES, get_pollutant, get_value_max

VALUE_MIN = 0.0


def parse_concentration(raw):
    """Parse a submitted reading into a finite float, or raise ValueError."""
    if isinstance(raw, bool):
        # bool 是 int 的子类, 显式拒绝避免 True/False 被当成 1/0
        raise ValueError("not_a_number")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("not_a_number")
    if not math.isfinite(value):
        raise ValueError("not_a_number")
    return value


def validate_value(pollutant_code, raw):
    """Validate one reading.

    返回 ``(value, error)``: 合法时 error 为 None; 非法时 value 为 None,
    error 为机器可读的错误码: ``not_a_number`` / ``negative`` / ``out_of_range``。
    """
    meta = get_pollutant(pollutant_code)
    if meta is None:
        return None, "unknown_pollutant"
    try:
        value = parse_concentration(raw)
    except ValueError:
        return None, "not_a_number"
    if value < VALUE_MIN:
        return None, "negative"
    value_max = get_value_max(pollutant_code)
    if value_max is not None and value > value_max:
        return None, "out_of_range"
    return value, None


def describe_error(meta, error):
    """Turn an error code into a Chinese message for a known pollutant."""
    label = meta["label"]
    unit = meta["unit"]
    if error == "not_a_number":
        return "%s 监测值必须是有限数字" % label
    if error == "negative":
        return "%s 监测值不能为负数" % label
    if error == "out_of_range":
        return "%s 监测值超出合理范围(0 ~ %s %s), 请检查是否录错" % (
            label,
            _format_bound(meta["value_max"]),
            unit,
        )
    return "%s 监测值不合法" % label


def _format_bound(value):
    return ("%f" % value).rstrip("0").rstrip(".")


def is_anomalous_value(pollutant_code, value):
    """Pure-python predicate: does this (pollutant, value) pair fail sanity checks?"""
    meta = get_pollutant(pollutant_code)
    if meta is None:
        return True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(number) or number < VALUE_MIN:
        return True
    return number > meta["value_max"]


def sql_anomaly_condition(measurement_model):
    """SQL predicate identifying legacy dirty rows with the SAME rule set.

    历史数据可能是在服务端校验上线前写入的, 因此不依赖写入时标记,
    每次统计都按当前口径即时识别: 未知因子、负值或超过因子量程上限。
    (Float 列不会存入 NaN/Inf, 故 SQL 侧只需覆盖这三类。)
    """
    bound = case(
        {
            measurement_model.pollutant == code: get_value_max(code)
            for code in POLLUTANT_CODES
        },
        else_=None,
    )
    return or_(
        measurement_model.pollutant.notin_(POLLUTANT_CODES),
        measurement_model.value < VALUE_MIN,
        measurement_model.value > bound,
    )
