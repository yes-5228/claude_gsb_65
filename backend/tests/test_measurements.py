"""监测数据录入接口测试."""
from datetime import datetime

from app.extensions import db
from app.models import Exceedance, Measurement


def test_batch_entry_creates_records_and_flags_exceedance(client, station, entry_payload):
    response = client.post("/api/measurements/entries", json=entry_payload(station.id))
    assert response.status_code == 201
    body = response.get_json()
    assert body["summary"]["created_count"] == 3
    assert body["summary"]["exceeded_count"] == 1
    assert len(body["exceedances"]) == 1
    assert body["exceedances"][0]["pollutant"] == "SO2"
    assert body["exceedances"][0]["status"] == "pending"
    assert body["station"]["code"] == "TEST-001"

    stored = Measurement.query.filter_by(pollutant="SO2").one()
    assert stored.is_exceeded is True
    assert stored.limit_value == 500.0
    assert stored.exceed_ratio == 1.8
    assert stored.unit == "μg/m³"
    assert stored.recorder == "测试员"


def test_duplicate_entry_is_reported_as_conflict(client, station, entry_payload):
    payload = entry_payload(station.id)
    client.post("/api/measurements/entries", json=payload)
    response = client.post("/api/measurements/entries", json=payload)
    assert response.status_code == 409
    assert "覆盖已有数据" in response.get_json()["error"]["message"]
    assert Measurement.query.count() == 3


def test_overwrite_updates_record_and_clears_exceedance(client, station, entry_payload):
    client.post("/api/measurements/entries", json=entry_payload(station.id))
    assert Exceedance.query.count() == 1

    response = client.post(
        "/api/measurements/entries",
        json=entry_payload(
            station.id,
            overwrite=True,
            entries=[{"pollutant": "SO2", "value": 120.0}],
        ),
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["summary"]["created_count"] == 0
    assert body["summary"]["updated_count"] == 1
    assert body["summary"]["exceeded_count"] == 0
    assert Measurement.query.filter_by(pollutant="SO2").one().is_exceeded is False
    assert Exceedance.query.count() == 0


def test_preview_validates_without_writing(client, station, entry_payload):
    payload = entry_payload(
        station.id,
        period="daily",
        entries=[{"pollutant": "PM25", "value": 90.0}, {"pollutant": "O3", "value": 100.0}],
    )
    payload.pop("station_id")
    response = client.post("/api/measurements/preview", json=payload)
    assert response.status_code == 200
    body = response.get_json()
    assert body["summary"] == {"total": 2, "exceeded_count": 1, "exceeded_pollutants": ["PM25"]}
    assert body["results"][0]["limit"] == 75.0
    assert body["results"][0]["level"] == "light"
    assert Measurement.query.count() == 0


def test_invalid_entries_are_rejected(client, station, entry_payload):
    unknown = client.post(
        "/api/measurements/entries",
        json=entry_payload(station.id, entries=[{"pollutant": "XX", "value": 1}]),
    )
    assert unknown.status_code == 422

    non_numeric = client.post(
        "/api/measurements/entries",
        json=entry_payload(station.id, entries=[{"pollutant": "PM25", "value": "abc"}]),
    )
    assert non_numeric.status_code == 422

    empty = client.post("/api/measurements/entries", json=entry_payload(station.id, entries=[]))
    assert empty.status_code == 422

    bad_station = client.post(
        "/api/measurements/entries", json=entry_payload(9999, entries=[{"pollutant": "PM25", "value": 10}])
    )
    assert bad_station.status_code == 404


def test_negative_value_rejected_server_side_without_any_record(client, station, entry_payload):
    """绕过页面直接 POST 负数: 整批 422, 不留任何记录, 不产生超标单。"""
    response = client.post(
        "/api/measurements/entries",
        json=entry_payload(
            station.id,
            entries=[
                {"pollutant": "PM25", "value": 60.0},
                {"pollutant": "SO2", "value": -12.0},
            ],
        ),
    )
    assert response.status_code == 422
    error = response.get_json()["error"]
    assert "负数" in error["message"] or error["fields"].get("SO2") == "negative"
    assert Measurement.query.count() == 0
    assert Exceedance.query.count() == 0


def test_implausibly_large_value_rejected_server_side(client, station, entry_payload):
    """极大值即使能算超标倍数也不能保存, 更不能作为达标/超标数据进入统计。"""
    response = client.post(
        "/api/measurements/entries",
        json=entry_payload(station.id, entries=[{"pollutant": "SO2", "value": 999999}]),
    )
    assert response.status_code == 422
    assert Measurement.query.count() == 0
    assert Exceedance.query.count() == 0


def test_nan_and_infinity_rejected(client, station, entry_payload):
    for bad in ("NaN", "Infinity", "-Infinity"):
        response = client.post(
            "/api/measurements/entries",
            json=entry_payload(station.id, entries=[{"pollutant": "PM25", "value": bad}]),
        )
        assert response.status_code == 422
    bool_response = client.post(
        "/api/measurements/entries",
        json=entry_payload(station.id, entries=[{"pollutant": "PM25", "value": True}]),
    )
    assert bool_response.status_code == 422
    assert Measurement.query.count() == 0


def test_preview_also_rejects_implausible_values(client, station, entry_payload):
    response = client.post(
        "/api/measurements/preview",
        json={"period": "hourly", "entries": [{"pollutant": "PM25", "value": -3}]},
    )
    assert response.status_code == 422
    assert Measurement.query.count() == 0


def test_value_policy_exposed(client):
    body = client.get("/api/measurements/value-policy").get_json()
    assert body["min_value"] == 0.0
    assert body["max_value_by_pollutant"]["PM25"] == 1000.0


def test_batch_import_uses_same_gate_and_is_atomic(client, station, second_station, entry_payload):
    """批量导入入口: 任一行非法 -> 整批拒绝, 任何一组都不留记录。"""
    payload = {
        "overwrite": False,
        "groups": [
            {
                "station_id": station.id,
                "measured_at": "2026-09-02 08:00",
                "period": "hourly",
                "data_source": "import",
                "entries": [{"pollutant": "PM25", "value": 55.0}],
            },
            {
                "station_id": second_station.id,
                "measured_at": "2026-09-02 09:00",
                "period": "hourly",
                "data_source": "import",
                "entries": [{"pollutant": "SO2", "value": -1.0}],
            },
        ],
    }
    response = client.post("/api/measurements/import", json=payload)
    assert response.status_code == 422
    fields = response.get_json()["error"]["fields"]
    assert any(key.startswith("groups[1]") for key in fields)
    assert Measurement.query.count() == 0
    assert Exceedance.query.count() == 0


def test_batch_import_success_single_transaction(client, station, second_station):
    payload = {
        "groups": [
            {
                "station_id": station.id,
                "measured_at": "2026-09-02 08:00",
                "period": "hourly",
                "entries": [{"pollutant": "PM25", "value": 55.0}],
            },
            {
                "station_id": second_station.id,
                "measured_at": "2026-09-02 09:00",
                "period": "daily",
                "data_source": "device",
                "entries": [{"pollutant": "SO2", "value": 900.0}],
            },
        ]
    }
    response = client.post("/api/measurements/import", json=payload)
    assert response.status_code == 201
    body = response.get_json()
    assert body["group_count"] == 2
    assert body["summary"]["created_count"] == 2
    assert body["summary"]["exceeded_count"] == 1
    assert Measurement.query.filter_by(is_valid=True).count() == 2


def test_batch_import_rejects_too_large_and_bad_source(client, station):
    base = {
        "station_id": station.id,
        "measured_at": "2026-09-02 08:00",
        "period": "hourly",
        "entries": [{"pollutant": "PM25", "value": 10}],
    }
    bad_source = client.post(
        "/api/measurements/import",
        json={"groups": [{**base, "data_source": "hacker"}]},
    )
    assert bad_source.status_code == 422
    assert Measurement.query.count() == 0

    not_list = client.post("/api/measurements/import", json={"groups": [{"nope": True}]})
    assert not_list.status_code == 422


def test_correction_revalidates_and_recalculates_statistics(client, station, entry_payload):
    """历史脏数据修正后: 有效标记恢复 + 超标按同口径重算 + 达标率回到正确值。"""
    client.post(
        "/api/measurements/entries",
        json=entry_payload(
            station.id,
            entries=[
                {"pollutant": "PM25", "value": 60.0},
                {"pollutant": "SO2", "value": 100.0},
            ],
        ),
    )
    # 模拟上线前混入的脏数据 (直接落库 + 质量扫描标记)。
    bad = Measurement(
        station_id=station.id, pollutant="SO2", period="hourly",
        measured_at=datetime(2026, 9, 2, 10, 0), value=-9.0, unit="μg/m³",
        is_exceeded=False, is_valid=True, data_source="manual",
    )
    db.session.add(bad)
    db.session.commit()
    bad_id = bad.id

    scan = client.post("/api/measurements/quality-scan", json={}).get_json()
    assert scan["flagged"] == 1
    flagged = client.get("/api/measurements?quality=invalid").get_json()
    assert flagged["total"] == 1
    assert flagged["items"][0]["invalid_reason"]

    # 未修正前: 统计口径只有 2 条有效数据, 全部达标 (SO2 100 < 500)。
    summary = client.get("/api/measurements/summary").get_json()
    assert summary["valid_count"] == 2
    assert summary["invalid_count"] == 1
    assert summary["compliance_rate"] == 1.0

    # 修正为真实超标值: 同口径重算, 达标率变为 0.5, 超标单自动建立。
    corrected = client.patch(
        "/api/measurements/%d/correct" % bad_id, json={"value": 900.0, "note": "仪器负数已复核"}
    )
    assert corrected.status_code == 200
    assert corrected.get_json()["is_valid"] is True
    assert corrected.get_json()["is_exceeded"] is True
    assert Exceedance.query.count() == 1

    summary_after = client.get("/api/measurements/summary").get_json()
    assert summary_after["valid_count"] == 3
    assert summary_after["invalid_count"] == 0
    assert summary_after["valid_exceeded_count"] == 1
    assert summary_after["compliance_rate"] == round(1 - 1 / 3, 4)


def test_correction_rejects_invalid_new_value(client, station):
    record = Measurement(
        station_id=station.id, pollutant="PM25", period="hourly",
        measured_at=datetime(2026, 9, 2, 11, 0), value=-1.0, is_valid=False,
        invalid_reason="负数", data_source="manual",
    )
    db.session.add(record)
    db.session.commit()
    response = client.patch(
        "/api/measurements/%d/correct" % record.id, json={"value": -2}
    )
    assert response.status_code == 422


def test_statistics_exclude_invalid_and_report_them_separately(client, station, entry_payload):
    client.post(
        "/api/measurements/entries",
        json=entry_payload(station.id, entries=[{"pollutant": "SO2", "value": 100.0}]),
    )
    bad = Measurement(
        station_id=station.id, pollutant="SO2", period="hourly",
        measured_at=datetime(2026, 9, 2, 12, 0), value=999999.0, unit="μg/m³",
        is_exceeded=True, exceed_ratio=9999.0, is_valid=False,
        invalid_reason="量程", data_source="manual",
    )
    db.session.add(bad)
    db.session.commit()

    stats = client.get("/api/query/statistics?group_by=pollutant").get_json()
    item = next(row for row in stats["items"] if row["key"] == "SO2")
    assert item["count"] == 1  # 有效条数, 极大值不计入
    assert item["invalid_count"] == 1
    assert item["exceeded_count"] == 0
    assert item["compliance_rate"] == 1.0
    assert stats["totals"]["invalid_count"] == 1

    quality = client.get("/api/measurements/quality-summary").get_json()
    assert quality["invalid_count"] == 1
    assert quality["by_reason_kind"]["too_large"] == 1
    assert quality["by_pollutant"][0]["pollutant"] == "SO2"


def test_exceedance_ranking_excludes_invalid(client, station, second_station, entry_payload):
    """脏数据产生的假超标不能进入高发站点排名和工作台统计。"""
    client.post(
        "/api/measurements/entries",
        json=entry_payload(station.id, entries=[{"pollutant": "SO2", "value": 100.0}]),
    )
    # second_station 只有一条极大脏数据 + 假超标单
    bad = Measurement(
        station_id=second_station.id, pollutant="SO2", period="hourly",
        measured_at=datetime(2026, 9, 2, 13, 0), value=999999.0, unit="μg/m³",
        is_exceeded=True, exceed_ratio=1999.0, is_valid=False,
        invalid_reason="量程", data_source="manual",
    )
    db.session.add(bad)
    db.session.flush()
    db.session.add(
        Exceedance(
            measurement_id=bad.id, station_id=second_station.id, pollutant="SO2",
            period="hourly", measured_at=bad.measured_at, value=bad.value,
            limit_value=500.0, exceed_ratio=1999.0, level="severe", status="pending",
        )
    )
    db.session.commit()

    summary = client.get("/api/exceedances/summary").get_json()
    assert summary["total"] == 0
    assert summary["invalid_count"] == 1
    assert [row["station_id"] for row in summary["top_stations"]] == []

    # 列表默认不显示异常超标单, 显式 quality=invalid 时可查
    assert client.get("/api/exceedances").get_json()["total"] == 0
    assert client.get("/api/exceedances?quality=invalid").get_json()["total"] == 1



def test_hourly_particulate_is_stored_without_limit(client, station, entry_payload):
    response = client.post(
        "/api/measurements/entries",
        json=entry_payload(station.id, entries=[{"pollutant": "PM10", "value": 300.0}]),
    )
    assert response.status_code == 201
    record = Measurement.query.filter_by(pollutant="PM10").one()
    assert record.limit_value is None
    assert record.is_exceeded is False
    assert Exceedance.query.count() == 0


def test_list_measurements_with_filters(client, station, entry_payload):
    client.post("/api/measurements/entries", json=entry_payload(station.id))
    body = client.get("/api/measurements?station_id=%d&pollutant=SO2" % station.id).get_json()
    assert body["total"] == 1
    assert body["items"][0]["pollutant_label"] == "SO₂"
    assert body["items"][0]["station"]["code"] == "TEST-001"
    assert body["summary"]["exceeded_count"] == 1

    exceeded = client.get("/api/measurements?is_exceeded=true").get_json()
    assert exceeded["total"] == 1


def test_delete_measurement_removes_exceedance(client, station, entry_payload):
    created = client.post("/api/measurements/entries", json=entry_payload(station.id)).get_json()
    exceeded_id = created["exceedances"][0]["measurement_id"]
    response = client.delete("/api/measurements/%d" % exceeded_id)
    assert response.status_code == 200
    assert Exceedance.query.count() == 0
    assert Measurement.query.count() == 2


def test_entry_context_exposes_form_options(client, station):
    body = client.get("/api/measurements/entry-context").get_json()
    assert body["stations"][0]["code"] == "TEST-001"
    assert {item["value"] for item in body["periods"]} == {"hourly", "daily"}
    assert {item["value"] for item in body["data_sources"]} >= {"manual", "device"}


def test_export_measurements_csv(client, station, entry_payload):
    client.post("/api/measurements/entries", json=entry_payload(station.id))
    response = client.get("/api/measurements/export?station_id=%d" % station.id)
    assert response.status_code == 200
    assert "text/csv" in response.headers["Content-Type"]
    text = response.get_data(as_text=True)
    assert text.startswith("\ufeff站点编码")
    assert "测试监测点" in text
    assert len([line for line in text.strip().splitlines()]) == 4
