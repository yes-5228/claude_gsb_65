"""监测数据录入业务逻辑 (含超标自动判定).

所有写库入口 (手工成组录入 / 批量粘贴 / 文件导入) 共用同一套校验与判定:
1. 先对整批数据做解析与合理性校验 (domain.value_gate), 任一失败整批拒绝, 不留记录;
2. 校验通过后才写库并在单个事务内提交, 异常时回滚, 不污染任何统计结果。
"""
from flask import current_app

from ..domain import exceedance_rules, value_gate
from ..domain.constants import DATA_SOURCE_LABELS
from ..domain.standards import get_pollutant
from ..errors import ConflictError, NotFoundError, ValidationError
from ..extensions import db
from ..models import Exceedance, Measurement, Station
from ..utils.validation import parse_datetime

_VALUE_ERROR_MESSAGES = {
    "not_number": "监测值必须为数字",
    "nan_infinite": "监测值不能为 NaN 或无穷大",
    "negative": "监测值不能为负数",
    "too_large": "监测值超过该因子可信量程上限",
}


def get_measurement(measurement_id):
    measurement = db.session.get(Measurement, measurement_id)
    if measurement is None:
        raise NotFoundError("监测数据不存在: id=%s" % measurement_id)
    return measurement


def _validated_entry(period, entry, field_key=None):
    """Parse + gate a single entry. ``field_key`` scopes the returned field error."""
    pollutant = str(entry.get("pollutant", "")).upper()
    meta = get_pollutant(pollutant)
    if meta is None:
        raise ValidationError(
            "未知监测因子: %s" % entry.get("pollutant"),
            fields={field_key or "pollutant": "unknown"},
        )

    key = field_key or pollutant
    raw_value = entry.get("value")
    if isinstance(raw_value, bool):
        raise ValidationError(
            "%s 监测值必须为数字" % meta["label"], fields={key: "invalid_number"}
        )
    value, error_code = value_gate.validate_value(pollutant, raw_value)
    if error_code in ("not_number", "nan_infinite"):
        raise ValidationError(
            "%s 监测值必须为有效数字" % meta["label"], fields={key: error_code}
        )
    if error_code is not None:
        upper = value_gate.plausible_max(pollutant)
        if error_code == "too_large":
            message = "%s 监测值 %s 超过可信量程上限 %s, 请检查单位或小数点" % (
                meta["label"], raw_value, upper,
            )
        elif error_code == "negative":
            message = "%s 监测值不能为负数: %s" % (meta["label"], raw_value)
        else:
            message = "%s %s" % (meta["label"], _VALUE_ERROR_MESSAGES.get(error_code, "监测值不合法"))
        raise ValidationError(message, fields={key: error_code})

    evaluation = exceedance_rules.evaluate(pollutant, period, value)
    return {
        "pollutant": pollutant,
        "pollutant_label": meta["label"],
        "value": value,
        "meta": meta,
        "unit": meta["unit"],
        **evaluation,
    }


def _validate_period(period):
    if period not in ("hourly", "daily"):
        raise ValidationError("未知数据周期: %s" % period, fields={"period": "unknown"})


def preview_entries(period, entries):
    """Dry-run evaluation for the entry form (no database writes)."""
    _validate_period(period)
    if not entries:
        raise ValidationError("至少需要一条监测数据", fields={"entries": "empty"})
    results = []
    seen = set()
    for entry in entries:
        validated = _validated_entry(period, entry)
        if validated["pollutant"] in seen:
            raise ValidationError(
                "%s 在同一时刻重复提交" % validated["pollutant_label"],
                fields={validated["pollutant"]: "duplicated_in_batch"},
            )
        seen.add(validated["pollutant"])
        results.append(validated)
    return {"period": period, "results": results, "summary": exceedance_rules.summarize(results)}


def _load_station(station_id):
    station = db.session.get(Station, station_id)
    if station is None:
        raise NotFoundError("监测点不存在: id=%s" % station_id)
    return station


def record_entries(station_id, measured_at, period, entries, data_source="manual",
                   recorder=None, remark=None, overwrite=False, commit=True):
    """Persist one measured_at snapshot for a station.

    Duplicate (station, pollutant, period, measured_at) rows are reported back;
    when ``overwrite`` is true the existing row is refreshed instead.
    Validation runs for the whole batch before any row is touched, so a rejected
    batch never leaves partial records behind.
    """
    _validate_period(period)
    station = _load_station(station_id)
    if not entries:
        raise ValidationError("至少需要录入一条监测数据", fields={"entries": "empty"})

    # ---- phase 1: validate everything (no writes) -------------------
    validated_entries = []
    seen = set()
    for entry in entries:
        validated = _validated_entry(period, entry)
        pollutant = validated["pollutant"]
        if pollutant in seen:
            raise ValidationError(
                "%s 在同一时刻重复提交" % validated["pollutant_label"],
                fields={pollutant: "duplicated_in_batch"},
            )
        seen.add(pollutant)
        validated["recorder"] = entry.get("recorder")
        validated["remark"] = entry.get("remark")
        validated_entries.append(validated)

    existing = {
        row.pollutant: row
        for row in Measurement.query.filter_by(
            station_id=station.id, period=period, measured_at=measured_at
        ).all()
    }

    # ---- phase 2: persist (single transaction) ----------------------
    created, updated, exceeded, duplicates, evaluated = [], [], [], [], []
    for validated in validated_entries:
        pollutant = validated["pollutant"]
        evaluation = validated
        evaluated.append(evaluation)

        record = existing.get(pollutant)
        if record is not None and not overwrite:
            duplicates.append(
                {
                    "pollutant": pollutant,
                    "pollutant_label": validated["pollutant_label"],
                    "value": validated["value"],
                    "existing_id": record.id,
                    "message": "该时刻 %s 数据已存在" % validated["pollutant_label"],
                }
            )
            continue

        is_new = record is None
        if is_new:
            record = Measurement(station_id=station.id, pollutant=pollutant, period=period,
                                 measured_at=measured_at)
            db.session.add(record)

        record.value = validated["value"]
        record.unit = validated["unit"]
        record.limit_value = evaluation["limit"]
        record.exceed_ratio = evaluation["ratio"]
        record.is_exceeded = evaluation["exceeded"]
        # 经过闸门的新写入一律是有效数据 (覆盖修正历史脏值时同步恢复)。
        record.is_valid = True
        record.invalid_reason = None
        record.data_source = data_source
        record.recorder = evaluation.get("recorder") or recorder
        record.remark = evaluation.get("remark") or remark

        _sync_exceedance(record, validated["meta"], evaluation)
        db.session.flush()
        (created if is_new else updated).append(record.to_dict(include_station=True))
        if evaluation["exceeded"]:
            exceeded.append(record.exceedance.to_dict() if record.exceedance else None)

    if not created and not updated and duplicates:
        raise ConflictError(
            "所选时刻已存在相同数据, 如需覆盖请勾选\"覆盖已有数据\": %s"
            % ", ".join(item["pollutant_label"] for item in duplicates)
        )

    if commit:
        db.session.commit()
    return {
        "station": station.to_option(),
        "measured_at": measured_at.isoformat(timespec="seconds"),
        "period": period,
        "created": created,
        "updated": updated,
        "exceedances": [item for item in exceeded if item],
        "duplicates": duplicates,
        "evaluations": evaluated,
        "summary": {
            "created_count": len(created),
            "updated_count": len(updated),
            "exceeded_count": len([item for item in evaluated if item["exceeded"]]),
            "duplicate_count": len(duplicates),
        },
    }


def record_entry_batches(groups, overwrite=False):
    """Persist multiple station/time snapshots in one transaction.

    Used by the batch paste / file import entry points. Every group is
    validated first; any invalid row aborts the whole request and nothing is
    written (all groups share the exact same gate as the manual form).
    """
    if not groups:
        raise ValidationError("至少需要一组录入数据", fields={"groups": "empty"})

    # ---- phase 1: normalise + validate all groups (no writes) --------
    plans = []
    total_rows = 0
    max_rows = current_app.config["MAX_BATCH_SIZE"]
    for index, group in enumerate(groups):
        prefix = "groups[%d]" % index
        if not isinstance(group, dict):
            raise ValidationError(
                "第 %d 行必须是对象" % (index + 1), fields={prefix: "invalid"}
            )
        station_id = group.get("station_id")
        try:
            station_id = int(station_id)
        except (TypeError, ValueError):
            raise ValidationError(
                "第 %d 行监测点不合法" % (index + 1), fields={prefix + ".station_id": "invalid"}
            )
        station = _load_station(station_id)

        period = str(group.get("period") or "hourly").strip()
        _validate_period(period)
        measured_at = parse_datetime(group.get("measured_at"), "第 %d 行监测时间" % (index + 1))

        data_source = str(group.get("data_source") or "import").strip()
        if data_source not in DATA_SOURCE_LABELS:
            raise ValidationError(
                "第 %d 行数据来源不合法: %s" % (index + 1, data_source),
                fields={prefix + ".data_source": "unknown"},
            )
        recorder = group.get("recorder")
        if recorder is not None and len(str(recorder)) > 64:
            raise ValidationError(
                "第 %d 行录入人长度不能超过 64 个字符" % (index + 1),
                fields={prefix + ".recorder": "too_long"},
            )

        raw_entries = group.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ValidationError(
                "第 %d 行至少需要一条监测数据" % (index + 1),
                fields={prefix + ".entries": "empty"},
            )
        total_rows += len(raw_entries)
        if total_rows > max_rows:
            raise ValidationError(
                "单次导入最多 %d 条监测数据" % max_rows, fields={"groups": "too_many"}
            )

        seen = set()
        for entry_index, entry in enumerate(raw_entries):
            if not isinstance(entry, dict):
                raise ValidationError(
                    "第 %d 行第 %d 条数据格式不正确" % (index + 1, entry_index + 1),
                    fields={"%s.entries[%d]" % (prefix, entry_index): "invalid"},
                )
            field_key = "%s.entries[%d]" % (prefix, entry_index)
            validated = _validated_entry(period, entry, field_key=field_key)
            if validated["pollutant"] in seen:
                raise ValidationError(
                    "第 %d 行 %s 重复" % (index + 1, validated["pollutant_label"]),
                    fields={field_key + ".pollutant": "duplicated_in_batch"},
                )
            seen.add(validated["pollutant"])

        plans.append(
            {
                "station": station,
                "station_id": station.id,
                "measured_at": measured_at,
                "period": period,
                "data_source": data_source,
                "recorder": recorder,
                "remark": group.get("remark"),
                "entries": raw_entries,
            }
        )

    # ---- phase 2: persist all groups in one transaction --------------
    results = []
    try:
        for plan in plans:
            result = record_entries(
                station_id=plan["station_id"],
                measured_at=plan["measured_at"],
                period=plan["period"],
                entries=plan["entries"],
                data_source=plan["data_source"],
                recorder=plan["recorder"],
                remark=plan["remark"],
                overwrite=overwrite,
                commit=False,
            )
            results.append(result)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    created_count = sum(item["summary"]["created_count"] for item in results)
    updated_count = sum(item["summary"]["updated_count"] for item in results)
    exceeded_count = sum(item["summary"]["exceeded_count"] for item in results)
    duplicate_count = sum(item["summary"]["duplicate_count"] for item in results)
    return {
        "groups": results,
        "group_count": len(results),
        "summary": {
            "created_count": created_count,
            "updated_count": updated_count,
            "exceeded_count": exceeded_count,
            "duplicate_count": duplicate_count,
            "total_count": total_rows,
        },
    }


def _sync_exceedance(record, meta, evaluation):
    """Create / refresh / drop the exceedance row attached to a measurement."""
    if evaluation["exceeded"]:
        if record.exceedance is None:
            record.exceedance = Exceedance(
                station_id=record.station_id,
                pollutant=record.pollutant,
                period=record.period,
                measured_at=record.measured_at,
                value=record.value,
                limit_value=evaluation["limit"],
                exceed_ratio=evaluation["ratio"],
                level=evaluation["level"],
                status="pending",
            )
        else:
            record.exceedance.value = record.value
            record.exceedance.limit_value = evaluation["limit"]
            record.exceedance.exceed_ratio = evaluation["ratio"]
            record.exceedance.level = evaluation["level"]
            record.exceedance.measured_at = record.measured_at
    elif record.exceedance is not None:
        db.session.delete(record.exceedance)


def delete_measurement(measurement):
    payload = measurement.to_dict()
    db.session.delete(measurement)
    db.session.commit()
    return payload
