"""监测数据录入业务逻辑 (含超标自动判定)."""
from datetime import datetime

from sqlalchemy import func

from ..domain import exceedance_rules
from ..domain.standards import get_pollutant
from ..domain.value_validation import describe_error, validate_value
from ..errors import ConflictError, NotFoundError, ValidationError
from ..extensions import db
from ..models import Exceedance, Measurement, Station


def get_measurement(measurement_id):
    measurement = db.session.get(Measurement, measurement_id)
    if measurement is None:
        raise NotFoundError("监测数据不存在: id=%s" % measurement_id)
    return measurement


def _validate_entry(entry, seen=None):
    """Validate a single entry row. Returns (pollutant, value, meta) or raises.

    数值合理域 (非数字 / 负数 / 超量程) 在此统一拦截, 页面表单、批量粘贴与导入共用。
    """
    if not isinstance(entry, dict):
        raise ValidationError(
            "每条数据必须包含 pollutant 与 value",
            fields={"entries": "invalid_row"},
        )
    pollutant = str(entry.get("pollutant", "")).upper()
    meta = get_pollutant(pollutant)
    if meta is None:
        raise ValidationError(
            "未知监测因子: %s" % entry.get("pollutant"),
            fields={pollutant or "pollutant": "unknown"},
        )
    if seen is not None:
        if pollutant in seen:
            raise ValidationError(
                "%s 在同一时刻重复提交" % meta["label"],
                fields={pollutant: "duplicated_in_batch"},
            )
        seen.add(pollutant)

    value, error = validate_value(pollutant, entry.get("value"))
    if error is not None:
        raise ValidationError(describe_error(meta, error), fields={pollutant: error})
    return pollutant, value, meta


def _collect_entry_errors(entries):
    """Validate the whole batch up front, collecting every bad row.

    返回 {字段: 消息}; 为空表示整批合法。任何写库动作发生之前调用,
    保证非法批次一条记录都不会留下。
    """
    errors = {}
    seen = set()
    for pos, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            errors["row_%d" % pos] = "每行必须是包含 pollutant 与 value 的对象"
            continue
        pollutant = str(entry.get("pollutant", "")).upper()
        meta = get_pollutant(pollutant)
        field = pollutant or "row_%d" % pos
        if meta is None:
            errors[field] = "未知监测因子: %s" % entry.get("pollutant")
            continue
        if pollutant in seen:
            errors[field] = "%s 在同一时刻重复提交" % meta["label"]
            continue
        seen.add(pollutant)
        value, error = validate_value(pollutant, entry.get("value"))
        if error is not None:
            errors[field] = describe_error(meta, error)
    return errors


def preview_entries(period, entries):
    """Dry-run evaluation for the entry form (no database writes)."""
    if not entries:
        raise ValidationError("至少需要录入一条监测数据", fields={"entries": "empty"})
    errors = _collect_entry_errors(entries)
    if errors:
        raise ValidationError("存在不合法的监测值, 已阻止预览", fields=errors)

    results = []
    for entry in entries:
        pollutant = str(entry.get("pollutant", "")).upper()
        meta = get_pollutant(pollutant)
        value = float(entry.get("value"))
        evaluation = exceedance_rules.evaluate(pollutant, period, value)
        results.append(
            {
                "pollutant": pollutant,
                "pollutant_label": meta["label"],
                "value": value,
                "unit": meta["unit"],
                "value_max": meta["value_max"],
                **evaluation,
            }
        )
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

    整批先做合理域校验, 任一因子非法直接抛 ValidationError, 不写任何记录。
    ``commit=False`` 供批量导入在多个分组间共用同一事务, 由调用方统一提交/回滚。
    """
    station = _load_station(station_id)
    if not entries:
        raise ValidationError("至少需要录入一条监测数据", fields={"entries": "empty"})

    errors = _collect_entry_errors(entries)
    if errors:
        raise ValidationError("存在不合法的监测值, 已阻止写入", fields=errors)

    existing = {
        row.pollutant: row
        for row in Measurement.query.filter_by(
            station_id=station.id, period=period, measured_at=measured_at
        ).all()
    }

    created, updated, exceeded, duplicates, evaluated = [], [], [], [], []
    seen = set()
    for entry in entries:
        pollutant, value, meta = _validate_entry(entry, seen=seen)

        evaluation = exceedance_rules.evaluate(pollutant, period, value)
        evaluated.append(
            {
                "pollutant": pollutant,
                "pollutant_label": meta["label"],
                "value": value,
                "unit": meta["unit"],
                **evaluation,
            }
        )

        record = existing.get(pollutant)
        if record is not None and not overwrite:
            duplicates.append(
                {
                    "pollutant": pollutant,
                    "pollutant_label": meta["label"],
                    "value": value,
                    "existing_id": record.id,
                    "message": "该时刻 %s 数据已存在" % meta["label"],
                }
            )
            continue

        is_new = record is None
        if is_new:
            record = Measurement(station_id=station.id, pollutant=pollutant, period=period,
                                 measured_at=measured_at)
            db.session.add(record)

        record.value = value
        record.unit = meta["unit"]
        record.limit_value = evaluation["limit"]
        record.exceed_ratio = evaluation["ratio"]
        record.is_exceeded = evaluation["exceeded"]
        record.data_source = data_source
        record.recorder = entry.get("recorder") or recorder
        record.remark = entry.get("remark") or remark

        _sync_exceedance(record, meta, evaluation)
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


def import_rows(rows, default_period="hourly", data_source="import", recorder=None,
                overwrite=False):
    """批量导入/粘贴入口: 多行 (站点 + 时刻 + 周期 + 因子 + 值) 原子写入。

    * 所有行先统一做存在性与合理域校验, 逐行报错;
    * 任一非法或 (未勾选覆盖时) 存在重复 => 整批拒绝并回滚, 不留任何记录;
    * 同一站点同一时刻的多个因子合并为一次成组录入, 超标判定与手工录入完全同口径。
    """
    if not rows:
        raise ValidationError("没有可导入的数据行", fields={"rows": "empty"})

    stations_cache = {}
    normalized = []
    errors = {}
    groups = {}

    def _station(code_or_id, row_no):
        token = code_or_id
        if isinstance(token, str) and token.strip().isdigit():
            token = int(token.strip())
        key = ("id", token) if isinstance(token, int) else ("code", str(token))
        if key in stations_cache:
            return stations_cache[key]
        kind, token = key
        station = (
            db.session.get(Station, token)
            if kind == "id"
            else Station.query.filter(func.lower(Station.code) == str(token).strip().lower()).first()
        )
        if station is None:
            errors["row_%d" % row_no] = "监测点不存在: %s" % key[1]
            stations_cache[key] = None
            return None
        stations_cache[key] = station
        return station

    for pos, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            errors["row_%d" % pos] = "每行必须是对象"
            continue
        station_token = row.get("station_id") if row.get("station_id") not in (None, "") else row.get("station_code")
        if station_token in (None, ""):
            errors["row_%d" % pos] = "缺少监测点"
            continue
        station = _station(station_token, pos)
        if station is None:
            continue

        period = str(row.get("period") or default_period or "hourly").strip()
        if period not in ("hourly", "daily"):
            errors["row_%d" % pos] = "数据周期不合法: %s" % period
            continue

        measured_at = row.get("measured_at")
        parsed_time = _parse_import_time(measured_at, pos, errors, period)
        if parsed_time is None:
            continue

        pollutant = str(row.get("pollutant", "")).upper()
        meta = get_pollutant(pollutant)
        if meta is None:
            errors["row_%d" % pos] = "未知监测因子: %s" % row.get("pollutant")
            continue
        value, error = validate_value(pollutant, row.get("value"))
        if error is not None:
            errors["row_%d" % pos] = describe_error(meta, error)
            continue

        group_key = (station.id, period, parsed_time)
        group = groups.setdefault(
            group_key,
            {"station": station, "period": period, "measured_at": parsed_time, "entries": []},
        )
        if any(item["pollutant"] == pollutant for item in group["entries"]):
            errors["row_%d" % pos] = "%s 在同一站点/时刻/周期中重复出现" % meta["label"]
            continue
        group["entries"].append(
            {
                "pollutant": pollutant,
                "value": value,
                "recorder": row.get("recorder"),
                "remark": row.get("remark"),
            }
        )
        normalized.append((pos, group_key))

    if errors:
        raise ValidationError("导入数据存在 %d 处问题, 已整批阻止写入" % len(errors), fields=errors)

    # 重复冲突预检 (未勾选覆盖时整批拒绝), 避免写入一半才发现冲突
    if not overwrite:
        conflicts = []
        for (station_id, period, measured_at), group in groups.items():
            codes = [item["pollutant"] for item in group["entries"]]
            existing = Measurement.query.filter_by(
                station_id=station_id, period=period, measured_at=measured_at
            ).filter(Measurement.pollutant.in_(codes)).all()
            for record in existing:
                conflicts.append("%s %s %s" % (record.station.code, record.pollutant_label(),
                                              measured_at.strftime("%Y-%m-%d %H:%M")))
        if conflicts:
            raise ConflictError(
                "以下数据已存在, 勾选\"覆盖已有数据\"后可重新导入: %s" % "; ".join(conflicts[:10])
            )

    results = []
    try:
        for (station_id, period, measured_at), group in groups.items():
            result = record_entries(
                station_id=station_id,
                measured_at=measured_at,
                period=period,
                entries=group["entries"],
                data_source=data_source,
                recorder=recorder,
                overwrite=overwrite,
                commit=False,
            )
            results.append(result)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    created = sum(item["summary"]["created_count"] for item in results)
    updated = sum(item["summary"]["updated_count"] for item in results)
    exceeded = sum(item["summary"]["exceeded_count"] for item in results)
    return {
        "group_count": len(results),
        "row_count": len(normalized),
        "created": created,
        "updated": updated,
        "exceeded_count": exceeded,
        "results": [
            {
                "station": item["station"],
                "measured_at": item["measured_at"],
                "period": item["period"],
                **item["summary"],
            }
            for item in results
        ],
        "summary": {
            "row_count": len(normalized),
            "group_count": len(results),
            "created_count": created,
            "updated_count": updated,
            "exceeded_count": exceeded,
        },
    }


def _parse_import_time(raw, row_no, errors, period=None):
    from ..utils.validation import parse_date, parse_datetime

    if raw in (None, ""):
        errors["row_%d" % row_no] = "缺少监测时间"
        return None
    text = str(raw).strip()
    # 日均值允许只填日期, 自动按 00:00 入库
    if period == "daily" and len(text) == 10:
        try:
            day = parse_date(text, "第 %d 行监测时间" % row_no)
            return datetime(day.year, day.month, day.day)
        except ValidationError as exc:
            errors["row_%d" % row_no] = exc.message
            return None
    try:
        return parse_datetime(text, "第 %d 行监测时间" % row_no)
    except ValidationError as exc:
        errors["row_%d" % row_no] = exc.message
        return None


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
