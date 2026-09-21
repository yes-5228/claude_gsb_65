"""数据质量治理: 识别历史脏数据并在修正后按同一口径恢复统计。

- ``scan_invalid_measurements``: 用服务端合理性闸门 (domain.value_gate) 全量
  回扫历史数据, 标记负值 / NaN / 超量程极大值。标记不删除数据, 只使其退出
  达标率统计与站点排名。
- ``correct_measurement``: 人工修正一条记录, 重新过闸门 + 超标判定,
  与正常录入走完全相同的计算口径, 达标率与排名随即自动重算。
- ``quality_summary``: 统计页"异常数据单独说明"所需的聚合信息。
"""
from sqlalchemy import func

from ..domain import exceedance_rules, value_gate
from ..errors import NotFoundError, ValidationError
from ..extensions import db
from ..models import Measurement

def get_measurement(measurement_id):
    record = db.session.get(Measurement, measurement_id)
    if record is None:
        raise NotFoundError("监测数据不存在: id=%s" % measurement_id)
    return record


def _flag(record, reason):
    record.is_valid = False
    record.invalid_reason = reason


def scan_invalid_measurements(rescan_all=False, batch_commit=True):
    """Sweep existing measurements through the server-side value gate.

    :param rescan_all: also re-evaluate rows already flagged invalid
                        (e.g. after the range policy was tightened).
    :returns: ``{"flagged": n, "already_flagged": n, "restored": n, "scanned": n}``
    """
    query = Measurement.query
    if not rescan_all:
        query = query.filter(Measurement.is_valid.is_(True))
    records = query.all()

    flagged, already_flagged, restored = 0, 0, 0
    for record in records:
        reason = value_gate.invalid_reason(record.pollutant, record.value)
        if reason is None:
            if rescan_all and not record.is_valid:
                # 人工或脚本改过值后重新变为合理数据: 恢复参与统计, 超标按现值重算。
                record.is_valid = True
                record.invalid_reason = None
                _refresh_evaluation(record)
                restored += 1
            continue
        if record.is_valid:
            _flag(record, reason)
            flagged += 1
        else:
            # 保留已有标记, 同时刷新原因文案 (量程口径可能更新过)。
            record.invalid_reason = reason
            already_flagged += 1
        if batch_commit and (flagged + already_flagged + restored) % 200 == 0:
            db.session.commit()
    db.session.commit()
    return {
        "scanned": len(records),
        "flagged": flagged,
        "already_flagged": already_flagged,
        "restored": restored,
    }


def _refresh_evaluation(record):
    """Recompute exceedance flags with the exact same logic as new entries."""
    evaluation = exceedance_rules.evaluate(record.pollutant, record.period, record.value)
    record.limit_value = evaluation["limit"]
    record.exceed_ratio = evaluation["ratio"]
    record.is_exceeded = evaluation["exceeded"]

    from ..services.measurement_service import _sync_exceedance
    from ..domain.standards import get_pollutant

    _sync_exceedance(record, get_pollutant(record.pollutant), evaluation)


def correct_measurement(measurement_id, value, note=None):
    """Manually correct a flagged (or any) measurement.

    The new value passes through the same value gate and exceedance rules as a
    normal entry, so compliance rate and rankings recompute on one 口径.
    """
    record = get_measurement(measurement_id)
    cleaned, error_code = value_gate.validate_value(record.pollutant, value)
    if error_code is not None:
        messages = {
            "not_number": "监测值必须为数字",
            "nan_infinite": "监测值不能为 NaN 或无穷大",
            "negative": "监测值不能为负数",
            "too_large": "监测值超过该因子可信量程上限 %s" % value_gate.plausible_max(record.pollutant),
        }
        raise ValidationError(
            messages.get(error_code, "监测值不合法"), fields={"value": error_code}
        )

    record.value = cleaned
    record.is_valid = True
    record.invalid_reason = None
    _refresh_evaluation(record)
    if note:
        record.remark = note
    db.session.commit()
    return record


def invalid_query(filters=None):
    """Base query of implausible measurements, optionally reusing query filters."""
    from ..services.query_service import apply_filters, parse_filters

    query = db.session.query(Measurement)
    if filters is not None:
        query = apply_filters(query, filters)
    return query.filter(Measurement.is_valid.is_(False))


def quality_summary(filters=None):
    """Aggregate counters for the 'abnormal data' section on statistics pages."""
    base = invalid_query(filters)
    total = base.count()

    by_pollutant = [
        {"pollutant": pollutant, "count": int(count)}
        for pollutant, count in (
            db.session.query(Measurement.pollutant, func.count(Measurement.id))
            .filter(Measurement.is_valid.is_(False))
            .group_by(Measurement.pollutant)
            .order_by(func.count(Measurement.id).desc())
            .all()
        )
    ]
    by_reason_kind = {"negative": 0, "too_large": 0, "other": 0}
    for record in db.session.query(Measurement.id, Measurement.invalid_reason).filter(
        Measurement.is_valid.is_(False)
    ).all():
        text_reason = record[1] or ""
        if "负数" in text_reason:
            by_reason_kind["negative"] += 1
        elif "量程" in text_reason:
            by_reason_kind["too_large"] += 1
        else:
            by_reason_kind["other"] += 1

    return {
        "invalid_count": int(total),
        "by_pollutant": by_pollutant,
        "by_reason_kind": by_reason_kind,
    }
