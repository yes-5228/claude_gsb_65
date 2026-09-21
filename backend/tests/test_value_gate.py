"""服务端数值合理性闸门测试: 绕过页面直接提交也必须被拦截。"""
import pytest

from app.domain import value_gate


@pytest.mark.parametrize(
    "pollutant,value",
    [
        ("PM25", -0.1),
        ("SO2", -999),
        ("CO", -0.001),
        ("PM25", 1000.1),
        ("PM10", 2000.1),
        ("SO2", 1000.1),
        ("CO", 100.01),
        ("O3", 100000),
    ],
)
def test_gate_rejects_negative_and_implausible(pollutant, value):
    cleaned, code = value_gate.validate_value(pollutant, value)
    assert cleaned is None
    assert code in {"negative", "too_large"}


@pytest.mark.parametrize(
    "pollutant,value",
    [
        ("PM25", 0),
        ("PM25", 75.0),
        ("PM10", 1999.9),
        ("SO2", 999.9),
        ("CO", 10.0),
        ("O3", 199.0),
    ],
)
def test_gate_accepts_plausible_values(pollutant, value):
    cleaned, code = value_gate.validate_value(pollutant, value)
    assert code is None
    assert cleaned == float(value)


def test_gate_rejects_nan_and_infinity():
    assert value_gate.validate_value("PM25", float("nan"))[1] == "nan_infinite"
    assert value_gate.validate_value("PM25", float("inf"))[1] == "nan_infinite"
    assert value_gate.validate_value("PM25", "abc")[1] == "not_number"
    assert value_gate.validate_value("PM25", None)[1] == "not_number"
    assert value_gate.validate_value("PM25", True)[1] == "not_number"


def test_gate_rejects_unknown_pollutant():
    assert value_gate.validate_value("XX", 1.0)[1] == "unknown_pollutant"


def test_invalid_reason_human_readable():
    assert "负数" in value_gate.invalid_reason("SO2", -5)
    assert "量程" in value_gate.invalid_reason("SO2", 5000)
    assert value_gate.invalid_reason("SO2", 100) is None
