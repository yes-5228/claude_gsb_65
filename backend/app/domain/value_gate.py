"""监测值合理范围判定 (服务端统一闸门).

页面端的 min/max 只是前置提示, 所有写库入口 (手工录入 / 批量粘贴 / 文件导入 /
设备接入) 都必须经过本模块校验, 防止绕过页面提交负值或明显不合理的极大值。

规则与具体限值标准 (GB 3095-2012) 解耦: 标准限值回答的是"是否超标",
这里回答的是"这个数在物理上是否可能被仪器测到"。
"""
import math

from .standards import get_pollutant

# 浓度不可能为负; 0 视为合法 (仪器零点 / 未检出可记 0)。
MIN_VALUE = 0.0

# 任何因子都不允许超过的绝对上限 (兜底, 防止小数点错位 / 单位错填)。
MAX_VALUE_ABSOLUTE = 10000.0

# 分因子的可信量程上限。依据各因子常规仪器量程与历史极端值留足余量,
# 显著超过该值的读数只可能是录入错误, 即使它"恰好"低于绝对上限。
MAX_VALUE_BY_POLLUTANT = {
    # 颗粒物: μg/m³, 环境空气中极少超过 1000, 沙尘暴极端情形亦不超过数千。
    "PM25": 1000.0,
    "PM10": 2000.0,
    # 气态污染物 (μg/m³): SO2/NO2/O3 常规量程均在千级以内。
    "SO2": 1000.0,
    "NO2": 1000.0,
    "O3": 1000.0,
    # CO: mg/m³, 环境空气典型量程 0~50 mg/m³。
    "CO": 100.0,
}


def plausible_max(pollutant_code):
    """Return the effective upper bound for a pollutant (None when unknown)."""
    pollutant = get_pollutant(pollutant_code)
    if pollutant is None:
        return None
    return min(
        MAX_VALUE_BY_POLLUTANT.get(pollutant["code"], MAX_VALUE_ABSOLUTE),
        MAX_VALUE_ABSOLUTE,
    )


def validate_value(pollutant_code, value):
    """Validate one reading.

    Returns ``(cleaned_value, None)`` when valid, otherwise
    ``(None, error_code)``.  Error codes:

    - ``not_number``: 无法解析为数字
    - ``nan_infinite``: NaN / Infinity
    - ``negative``: 负数
    - ``too_large``: 超过该因子可信量程
    - ``unknown_pollutant``
    """
    pollutant = get_pollutant(pollutant_code)
    if pollutant is None:
        return None, "unknown_pollutant"
    if isinstance(value, bool):
        # True/False 是合法 JSON 但不是监测数值。
        return None, "not_number"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, "not_number"
    if not math.isfinite(number):
        return None, "nan_infinite"
    if number < MIN_VALUE:
        return None, "negative"
    upper = MAX_VALUE_BY_POLLUTANT.get(pollutant["code"], MAX_VALUE_ABSOLUTE)
    if number > upper:
        return None, "too_large"
    return number, None


def invalid_reason(pollutant_code, value):
    """Return a human readable reason string when the reading is implausible."""
    if value is None:
        return None
    pollutant = get_pollutant(pollutant_code)
    if isinstance(value, bool):
        return "监测值不是有效数字"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "监测值不是有效数字"
    if not math.isfinite(number):
        return "监测值为 NaN 或无穷大"
    if number < MIN_VALUE:
        return "监测值为负数(%.4g), 不符合物理意义" % number
    if pollutant is None:
        return "未知监测因子: %s" % pollutant_code
    upper = MAX_VALUE_BY_POLLUTANT.get(pollutant["code"], MAX_VALUE_ABSOLUTE)
    if number > upper:
        return "监测值 %.4g 超过 %s 可信量程上限 %s" % (number, pollutant["label"], upper)
    return None


def value_policy():
    """Serialisable policy exposed to the frontend so bounds stay in one place."""
    return {
        "min_value": MIN_VALUE,
        "max_value_absolute": MAX_VALUE_ABSOLUTE,
        "max_value_by_pollutant": dict(MAX_VALUE_BY_POLLUTANT),
    }
