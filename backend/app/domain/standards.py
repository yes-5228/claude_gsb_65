"""污染物监测因子与限值定义 (GB 3095-2012 环境空气质量标准, 二级浓度限值)."""

# period 取值: hourly = 1 小时平均, daily = 24 小时平均
POLLUTANTS = {
    "PM25": {
        "code": "PM25",
        "label": "PM2.5",
        "name": "细颗粒物",
        "unit": "μg/m³",
        "precision": 1,
        "value_max": 1000.0,
        "limits": {"daily": 75.0, "hourly": None},
    },
    "PM10": {
        "code": "PM10",
        "label": "PM10",
        "name": "可吸入颗粒物",
        "unit": "μg/m³",
        "precision": 1,
        "value_max": 10000.0,
        "limits": {"daily": 150.0, "hourly": None},
    },
    "SO2": {
        "code": "SO2",
        "label": "SO₂",
        "name": "二氧化硫",
        "unit": "μg/m³",
        "precision": 1,
        "value_max": 10000.0,
        "limits": {"daily": 150.0, "hourly": 500.0},
    },
    "NO2": {
        "code": "NO2",
        "label": "NO₂",
        "name": "二氧化氮",
        "unit": "μg/m³",
        "precision": 1,
        "value_max": 10000.0,
        "limits": {"daily": 80.0, "hourly": 200.0},
    },
    "CO": {
        "code": "CO",
        "label": "CO",
        "name": "一氧化碳",
        "unit": "mg/m³",
        "precision": 2,
        "value_max": 100.0,
        "limits": {"daily": 4.0, "hourly": 10.0},
    },
    "O3": {
        "code": "O3",
        "label": "O₃",
        "name": "臭氧",
        "unit": "μg/m³",
        "precision": 1,
        "value_max": 10000.0,
        "limits": {"daily": 160.0, "hourly": 200.0},
    },
}

# value_max: 各因子监测浓度的物理合理量程上限 (与单位配套, 见各因子定义)。
# 上限远大于 GB 3095-2012 二级限值, 不会误伤真实超标数据, 仅用于拦截录错/脏数据。

POLLUTANT_CODES = tuple(POLLUTANTS.keys())


def get_pollutant(code):
    """Return the pollutant definition or None when unknown."""
    return POLLUTANTS.get(str(code or "").upper())


def get_value_max(code):
    """Return the sane upper bound for a pollutant reading (None when unknown)."""
    pollutant = get_pollutant(code)
    if pollutant is None:
        return None
    return pollutant.get("value_max")


def get_limit(code, period):
    """Return the concentration limit for a pollutant/period pair (None if undefined)."""
    pollutant = get_pollutant(code)
    if not pollutant:
        return None
    return pollutant["limits"].get(period)


def pollutant_options():
    """Serialisable list used by the frontend dropdowns."""
    return [dict(item) for item in POLLUTANTS.values()]
