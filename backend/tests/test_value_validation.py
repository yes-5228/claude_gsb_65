"""服务端数值合理域校验与历史异常值处理测试."""
from datetime import datetime

from app.domain.value_validation import (
    is_anomalous_value,
    sql_anomaly_condition,
    validate_value,
)
from app.extensions import db
from app.models import Exceedance, Measurement
from app.services import measurement_service, query_service


# ---------------------------------------------------------------- 领域规则

def test_validate_value_rules():
    assert validate_value("PM25", 35.0) == (35.0, None)
    assert validate_value("PM25", 0) == (0.0, None)
    assert validate_value("PM25", -0.1)[1] == "negative"
    assert validate_value("PM25", 1000.1)[1] == "out_of_range"
    assert validate_value("CO", 100.1)[1] == "out_of_range"
    assert validate_value("PM25", "abc")[1] == "not_a_number"
    assert validate_value("PM25", float("inf"))[1] == "not_a_number"
    assert validate_value("PM25", True)[1] == "not_a_number"
    assert validate_value("XX", 1)[1] == "unknown_pollutant"
    # 超过国标限值但仍在物理量程内的真实超标值必须放行
    assert validate_value("SO2", 900.0)[1] is None


def test_is_anomalous_value_predicate():
    assert is_anomalous_value("PM25", -1)
    assert is_anomalous_value("PM25", 5000)
    assert not is_anomalous_value("SO2", 900)
    assert is_anomalous_value("UNKNOWN", 1)


# ------------------------------------------------------- 录入接口服务端拦截

def test_entries_reject_negative_and_huge_values(client, station, entry_payload):
    for bad in (-10.0, 5000.0):
        response = client.post(
            "/api/measurements/entries",
            json=entry_payload(station.id, entries=[{"pollutant": "PM25", "value": bad}]),
        )
        assert response.status_code == 422
        body = response.get_json()
        assert "PM25" in body["error"]["fields"]
    # 失败不留任何记录, 也不产生超标单
    assert Measurement.query.count() == 0
    assert Exceedance.query.count() == 0


def test_entries_reject_non_finite_numbers(client, station, entry_payload):
    for bad in ("abc", "NaN", "Infinity"):
        response = client.post(
            "/api/measurements/entries",
            json=entry_payload(station.id, entries=[{"pollutant": "PM25", "value": bad}]),
        )
        assert response.status_code == 422
    assert Measurement.query.count() == 0


def test_one_bad_entry_aborts_the_whole_batch(client, station, entry_payload):
    response = client.post(
        "/api/measurements/entries",
        json=entry_payload(
            station.id,
            entries=[
                {"pollutant": "PM25", "value": 35.0},
                {"pollutant": "SO2", "value": -1.0},
            ],
        ),
    )
    assert response.status_code == 422
    assert Measurement.query.count() == 0


def test_preview_also_rejects_unreasonable_values(client, station, entry_payload):
    response = client.post(
        "/api/measurements/preview",
        json={"period": "hourly", "entries": [{"pollutant": "PM25", "value": -3}]},
    )
    assert response.status_code == 422
    assert Measurement.query.count() == 0


# ------------------------------------------------------------- 批量导入入口

def test_import_endpoint_validates_and_is_atomic(client, station):
    rows = [
        {"station_id": station.id, "measured_at": "2026-09-01 08:00",
         "period": "hourly", "pollutant": "PM25", "value": 40.0},
        {"station_id": station.id, "measured_at": "2026-09-01 08:00",
         "period": "hourly", "pollutant": "SO2", "value": 600.0},
        {"station_id": station.id, "measured_at": "2026-09-01 09:00",
         "period": "hourly", "pollutant": "CO", "value": -1.0},
    ]
    response = client.post("/api/measurements/imports", json={"rows": rows})
    assert response.status_code == 422
    assert response.get_json()["error"]["fields"]["row_3"].endswith("不能为负数")
    assert Measurement.query.count() == 0

    rows[2]["value"] = 1.0
    response = client.post("/api/measurements/imports", json={"rows": rows, "recorder": "导入员"})
    assert response.status_code == 201
    body = response.get_json()
    assert body["summary"] == {
        "row_count": 3, "group_count": 2,
        "created_count": 3, "updated_count": 0, "exceeded_count": 1,
    }
    assert Measurement.query.filter_by(recorder="导入员").count() == 3


def test_import_duplicate_without_overwrite_is_entirely_rejected(client, station):
    rows = [
        {"station_code": "TEST-001", "measured_at": "2026-09-01 08:00",
         "pollutant": "PM25", "value": 40.0},
    ]
    assert client.post("/api/measurements/imports", json={"rows": rows}).status_code == 201
    # 追加另一时刻的新数据 + 一条重复 => 整批拒绝, 新数据也不得落库
    rows.append({"station_code": "TEST-001", "measured_at": "2026-09-01 09:00",
                 "pollutant": "CO", "value": 0.8})
    response = client.post("/api/measurements/imports", json={"rows": rows})
    assert response.status_code == 409
    assert Measurement.query.count() == 1


def test_import_accepts_station_code_and_default_period(client, station):
    response = client.post(
        "/api/measurements/imports",
        json={
            "period": "daily",
            "rows": [{"station_code": "TEST-001", "measured_at": "2026-09-01",
                      "pollutant": "PM25", "value": 60.0}],
        },
    )
    assert response.status_code == 201
    record = Measurement.query.one()
    assert record.period == "daily"
    assert record.data_source == "import"


# ---------------------------------------------------- 历史脏数据识别与统计

def _insert_legacy_anomaly(station, pollutant="PM25", value=-9.0, exceeded=False,
                           measured_at=None):
    record = Measurement(
        station_id=station.id, pollutant=pollutant, period="hourly", value=value,
        unit="μg/m³", is_exceeded=exceeded,
        limit_value=500 if exceeded else None,
        exceed_ratio=round(value / 500, 3) if exceeded else None,
        measured_at=measured_at or datetime(2026, 8, 1, 10, 0),
        data_source="import",
    )
    db.session.add(record)
    db.session.commit()
    if exceeded:
        db.session.add(
            Exceedance(
                measurement_id=record.id, station_id=station.id, pollutant=pollutant,
                period="hourly", measured_at=record.measured_at, value=value,
                limit_value=500, exceed_ratio=record.exceed_ratio,
                level="severe", status="pending",
            )
        )
        db.session.commit()
    return record


def test_sql_anomaly_predicate_finds_legacy_dirty_rows(client, station):
    _insert_legacy_anomaly(station, "PM25", -9.0)
    _insert_legacy_anomaly(station, "SO2", 999999.0, exceeded=True)
    condition = sql_anomaly_condition(Measurement)
    assert Measurement.query.filter(condition).count() == 2
    # 真实超标 (900 < SO2 量程上限) 不能被误判为异常
    measurement_service.record_entries(
        station.id, datetime(2026, 9, 1, 10), "hourly",
        [{"pollutant": "SO2", "value": 900.0}],
    )
    assert Measurement.query.filter(condition).count() == 2


def test_statistics_exclude_anomalies_and_report_them_separately(client, station):
    _insert_legacy_anomaly(station, "PM25", -9.0)
    _insert_legacy_anomaly(station, "SO2", 999999.0, exceeded=True)
    measurement_service.record_entries(
        station.id, datetime(2026, 9, 1, 10), "hourly",
        [{"pollutant": "PM25", "value": 60.0}, {"pollutant": "SO2", "value": 900.0}],
    )

    summary = query_service.summary(query_service.parse_filters({}))
    assert summary["total"] == 2                 # 只统计有效数据
    assert summary["exceeded_count"] == 1
    assert summary["compliant_count"] == 1
    assert summary["exceed_rate"] == 0.5
    assert summary["compliance_rate"] == 0.5
    assert summary["anomaly_count"] == 2        # 异常单独说明

    # 列表默认不展示异常值, anomaly=only 可专门筛出
    listing = client.get("/api/query/measurements").get_json()
    assert listing["total"] == 2
    only = client.get("/api/query/measurements?anomaly=only").get_json()
    assert only["total"] == 2
    assert all(item["is_anomaly"] for item in only["items"])
    included = client.get("/api/query/measurements?anomaly=include").get_json()
    assert included["total"] == 4

    stats = query_service.statistics({"group_by": "pollutant", "metric": "avg"})
    pm25 = next(item for item in stats["items"] if item["key"] == "PM25")
    assert pm25["value"] == 60.0
    assert pm25["count"] == 1
    assert pm25["anomaly_count"] == 1
    assert stats["totals"] == {"count": 2, "exceeded_count": 1, "anomaly_count": 2}


def test_correcting_anomaly_recomputes_rates_and_rankings(client, station):
    dirty = _insert_legacy_anomaly(station, "SO2", 999999.0, exceeded=True,
                                   measured_at=datetime(2026, 9, 5, 10))
    # 修正前: 异常值不计入达标率, 也不计入超标排名
    before = client.get("/api/query/measurements").get_json()["summary"]
    assert before["total"] == 0
    assert before["exceed_rate"] is None
    assert before["anomaly_count"] == 1

    # 用正确数值覆盖重提 -> 超标记录同步刷新, 统计按同一口径重算
    response = client.post(
        "/api/measurements/entries",
        json={
            "station_id": station.id, "measured_at": "2026-09-05 10:00",
            "period": "hourly", "overwrite": True,
            "entries": [{"pollutant": "SO2", "value": 120.0}],
        },
    )
    assert response.status_code == 201
    after = client.get("/api/query/measurements").get_json()["summary"]
    assert after["total"] == 1
    assert after["exceeded_count"] == 0
    assert after["anomaly_count"] == 0
    assert db.session.get(Measurement, dirty.id).value == 120.0
    assert Exceedance.query.count() == 0

    # 删除异常值后同样不应留下统计痕迹
    other = _insert_legacy_anomaly(station, "PM25", -3.0)
    client.delete("/api/measurements/%d" % other.id)
    final = client.get("/api/query/measurements").get_json()["summary"]
    assert final["anomaly_count"] == 0
    assert final["total"] == 1


def test_exceedance_summary_excludes_anomalies(client, station):
    _insert_legacy_anomaly(station, "SO2", 999999.0, exceeded=True)
    measurement_service.record_entries(
        station.id, datetime(2026, 9, 1, 10), "hourly",
        [{"pollutant": "SO2", "value": 900.0}],
    )
    summary = client.get("/api/exceedances/summary").get_json()
    assert summary["total"] == 1           # 异常超标单不进排名
    assert summary["anomaly_count"] == 1
    assert summary["top_stations"][0]["count"] == 1
