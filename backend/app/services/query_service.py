"""监测数据查询: 过滤条件解析, 统计聚合与导出数据准备."""
from datetime import datetime, time

from sqlalchemy import cast, func, or_

from ..domain.constants import (
    DATA_SOURCE_LABELS,
    EXCEEDANCE_STATUS_LABELS,
    PERIOD_LABELS,
    STATION_TYPE_LABELS,
)
from ..domain.standards import POLLUTANT_CODES, get_pollutant
from ..domain.value_validation import sql_anomaly_condition
from ..errors import ValidationError
from ..extensions import db
from ..models import Exceedance, Measurement, Station
from ..models.base import iso
from ..utils.validation import parse_date

GROUP_BY_CHOICES = ("station", "area", "pollutant", "period", "day", "month", "data_source")
METRIC_CHOICES = ("avg", "max", "min", "count", "sum")
SORT_CHOICES = ("measured_at", "value", "exceed_ratio", "pollutant", "station_code", "created_at")
# 异常值筛选: exclude=统计口径默认, 排除录入错误; only=仅看历史脏数据; include=不做区分
ANOMALY_CHOICES = ("exclude", "only", "include")


def _split(value):
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _int_list(args, name):
    values = []
    for item in _split(args.get(name)):
        try:
            values.append(int(item))
        except ValueError:
            raise ValidationError("%s 参数必须为整数" % name, fields={name: "invalid_integer"})
    return values


def _float_arg(args, name):
    raw = args.get(name)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except ValueError:
        raise ValidationError("%s 参数必须为数字" % name, fields={name: "invalid_number"})


def _bool_arg(args, name):
    raw = args.get(name)
    if raw in (None, ""):
        return None
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _date_arg(args, name, end_of_day=False):
    raw = args.get(name)
    if raw in (None, ""):
        return None
    parsed = parse_date(raw, name)
    return datetime.combine(parsed, time.max if end_of_day else time.min)


def parse_filters(args):
    """Translate request args into a normalised filter dictionary."""
    pollutants = [item.upper() for item in _split(args.get("pollutant"))]
    unknown = [item for item in pollutants if item not in POLLUTANT_CODES]
    if unknown:
        raise ValidationError(
            "未知监测因子: %s" % ", ".join(unknown), fields={"pollutant": "unknown"}
        )

    periods = _split(args.get("period"))
    for period in periods:
        if period not in PERIOD_LABELS:
            raise ValidationError("未知数据周期: %s" % period, fields={"period": "unknown"})

    filters = {
        "station_ids": _int_list(args, "station_id"),
        "areas": _split(args.get("area")),
        "station_types": _split(args.get("station_type")),
        "pollutants": pollutants,
        "periods": periods,
        "data_sources": _split(args.get("data_source")),
        "is_exceeded": _bool_arg(args, "is_exceeded"),
        "anomaly": (args.get("anomaly") or "exclude").strip(),
        "exceedance_status": _split(args.get("exceedance_status")),
        "date_from": _date_arg(args, "date_from"),
        "date_to": _date_arg(args, "date_to", end_of_day=True),
        "min_value": _float_arg(args, "min_value"),
        "max_value": _float_arg(args, "max_value"),
        "keyword": (args.get("keyword") or "").strip(),
        "recorder": (args.get("recorder") or "").strip(),
    }
    if filters["date_from"] and filters["date_to"] and filters["date_from"] > filters["date_to"]:
        raise ValidationError(
            "开始时间不能晚于结束时间", fields={"date_from": "range_invalid"}
        )
    if (
        filters["min_value"] is not None
        and filters["max_value"] is not None
        and filters["min_value"] > filters["max_value"]
    ):
        raise ValidationError("最小值不能大于最大值", fields={"min_value": "range_invalid"})
    return filters


def apply_anomaly_filter(query, mode):
    """Apply the anomaly policy shared by list browsing and every statistic."""
    condition = sql_anomaly_condition(Measurement)
    if mode == "exclude":
        return query.filter(~condition)
    if mode == "only":
        return query.filter(condition)
    return query


def apply_filters(query, filters):
    query = query.join(Station, Measurement.station_id == Station.id)
    if filters["station_ids"]:
        query = query.filter(Measurement.station_id.in_(filters["station_ids"]))
    if filters["areas"]:
        query = query.filter(Station.area.in_(filters["areas"]))
    if filters["station_types"]:
        query = query.filter(Station.station_type.in_(filters["station_types"]))
    if filters["pollutants"]:
        query = query.filter(Measurement.pollutant.in_(filters["pollutants"]))
    if filters["periods"]:
        query = query.filter(Measurement.period.in_(filters["periods"]))
    if filters["data_sources"]:
        query = query.filter(Measurement.data_source.in_(filters["data_sources"]))
    query = apply_anomaly_filter(query, filters.get("anomaly", "exclude"))
    if filters["is_exceeded"] is not None:
        query = query.filter(Measurement.is_exceeded.is_(filters["is_exceeded"]))
    if filters["date_from"]:
        query = query.filter(Measurement.measured_at >= filters["date_from"])
    if filters["date_to"]:
        query = query.filter(Measurement.measured_at <= filters["date_to"])
    if filters["min_value"] is not None:
        query = query.filter(Measurement.value >= filters["min_value"])
    if filters["max_value"] is not None:
        query = query.filter(Measurement.value <= filters["max_value"])
    if filters["recorder"]:
        query = query.filter(Measurement.recorder.like("%" + filters["recorder"] + "%"))
    if filters["keyword"]:
        like = "%" + filters["keyword"] + "%"
        query = query.filter(
            or_(Station.name.like(like), Station.code.like(like), Station.address.like(like))
        )
    if filters["exceedance_status"]:
        query = query.join(Exceedance, Exceedance.measurement_id == Measurement.id).filter(
            Exceedance.status.in_(filters["exceedance_status"])
        )
    return query


def apply_sort(query, sort=None, order="desc"):
    sort = sort if sort in SORT_CHOICES else "measured_at"
    column = {
        "measured_at": Measurement.measured_at,
        "value": Measurement.value,
        "exceed_ratio": Measurement.exceed_ratio,
        "pollutant": Measurement.pollutant,
        "station_code": Station.code,
        "created_at": Measurement.created_at,
    }[sort]
    primary = column.desc() if (order or "desc").lower() == "desc" else column.asc()
    return query.order_by(primary, Measurement.id.desc())


def measurement_query(args):
    filters = parse_filters(args)
    query = apply_filters(db.session.query(Measurement), filters)
    return apply_sort(query, args.get("sort"), args.get("order")), filters


def summary(filters):
    """Aggregate counters shown above the query result table.

    达标率/超标率与平均值等指标只按有效数据 (排除异常值) 计算;
    同一筛选范围内的异常值单独计数, 供页面说明“另有 N 条异常值未参与统计”。
    """
    valid_filters = {**filters, "anomaly": "exclude"}
    query = apply_filters(
        db.session.query(
            func.count(Measurement.id),
            func.sum(cast(Measurement.is_exceeded, db.Integer)),
            func.count(func.distinct(Measurement.station_id)),
            func.min(Measurement.measured_at),
            func.max(Measurement.measured_at),
            func.avg(Measurement.value),
        ),
        valid_filters,
    )
    total, exceeded, stations, first_at, last_at, avg_value = query.one()
    total = int(total or 0)
    exceeded = int(exceeded or 0)
    anomaly_count = (
        apply_filters(db.session.query(func.count(Measurement.id)), {**filters, "anomaly": "only"})
        .scalar()
    )
    return {
        "total": total,
        "exceeded_count": exceeded,
        "compliant_count": total - exceeded,
        "exceed_rate": round(exceeded / total, 4) if total else None,
        "compliance_rate": round((total - exceeded) / total, 4) if total else None,
        "anomaly_count": int(anomaly_count or 0),
        "station_count": int(stations or 0),
        "first_measured_at": iso(first_at),
        "last_measured_at": iso(last_at),
        "avg_value": round(float(avg_value), 2) if avg_value is not None else None,
    }


def _metric_expression(metric):
    return {
        "avg": func.avg(Measurement.value),
        "max": func.max(Measurement.value),
        "min": func.min(Measurement.value),
        "count": func.count(Measurement.id),
        "sum": func.sum(Measurement.value),
    }[metric]


def _grouped_query(group_by, value_expr, with_valid_only):
    """Build the dimension-specific grouped query (without user filters)."""
    if group_by == "station":
        query = db.session.query(
            Station.id.label("station_id"),
            Station.code.label("station_code"),
            Station.name.label("station_name"),
            Station.area.label("area"),
            value_expr,
            func.count(Measurement.id).label("row_count"),
            func.sum(cast(Measurement.is_exceeded, db.Integer)).label("exceeded_count"),
        ).group_by(Station.id, Station.code, Station.name, Station.area)
        return False, query
    if group_by == "area":
        query = db.session.query(
            Station.area.label("area"), value_expr,
            func.count(Measurement.id).label("row_count"),
            func.sum(cast(Measurement.is_exceeded, db.Integer)).label("exceeded_count"),
        ).group_by(Station.area)
        return False, query
    if group_by == "day":
        bucket = func.date(Measurement.measured_at).label("bucket")
        query = db.session.query(
            bucket, value_expr,
            func.count(Measurement.id).label("row_count"),
            func.sum(cast(Measurement.is_exceeded, db.Integer)).label("exceeded_count"),
        ).group_by(bucket)
        return True, query
    if group_by == "month":
        year = func.extract("year", Measurement.measured_at).label("year")
        month = func.extract("month", Measurement.measured_at).label("month")
        query = db.session.query(
            year, month, value_expr,
            func.count(Measurement.id).label("row_count"),
            func.sum(cast(Measurement.is_exceeded, db.Integer)).label("exceeded_count"),
        ).group_by(year, month)
        return True, query
    column = {
        "pollutant": Measurement.pollutant,
        "period": Measurement.period,
        "data_source": Measurement.data_source,
    }[group_by]
    query = db.session.query(
        column.label("bucket"), value_expr,
        func.count(Measurement.id).label("row_count"),
        func.sum(cast(Measurement.is_exceeded, db.Integer)).label("exceeded_count"),
    ).group_by(column)
    return False, query


def statistics(args):
    """Grouped aggregation used by the query page statistics panel.

    所有指标 (avg/max/min/sum) 与达标率一律基于有效数据 (排除异常值);
    每个分组同时返回 anomaly_count, 与总体口径一致, 修正后自动重算。
    """
    filters = parse_filters(args)
    group_by = args.get("group_by") or "pollutant"
    metric = args.get("metric") or "avg"
    if group_by not in GROUP_BY_CHOICES:
        raise ValidationError(
            "group_by 仅支持: %s" % ", ".join(GROUP_BY_CHOICES), fields={"group_by": "unknown"}
        )
    if metric not in METRIC_CHOICES:
        raise ValidationError(
            "metric 仅支持: %s" % ", ".join(METRIC_CHOICES), fields={"metric": "unknown"}
        )

    value_expr = _metric_expression(metric).label("metric_value")
    is_time_group, query = _grouped_query(group_by, value_expr, with_valid_only=True)
    query = apply_filters(query, {**filters, "anomaly": "exclude"})
    rows = query.all()

    # 同维度的异常条数 (同口径单独说明, 不并入 count/超标率)
    _, anomaly_query = _grouped_query(group_by, func.count(Measurement.id).label("metric_value"),
                                      with_valid_only=True)
    anomaly_query = apply_filters(anomaly_query, {**filters, "anomaly": "only"})
    anomaly_map = {}
    for row in anomaly_query.all():
        data = dict(row._mapping)
        anomaly_map[_group_key(group_by, data)] = int(data.get("row_count") or 0)

    items = []
    for row in rows:
        data = dict(row._mapping)
        count = int(data.get("row_count") or 0)
        exceeded = int(data.get("exceeded_count") or 0)
        raw_value = data.get("metric_value")
        key, label = _group_key_label(group_by, data)
        items.append(
            {
                "key": key,
                "label": label,
                "value": round(float(raw_value), 2) if raw_value is not None else None,
                "count": count,
                "exceeded_count": exceeded,
                "compliant_count": count - exceeded,
                "exceed_rate": round(exceeded / count, 4) if count else None,
                "compliance_rate": round((count - exceeded) / count, 4) if count else None,
                "anomaly_count": anomaly_map.get(key, 0),
            }
        )

    if is_time_group:
        items.sort(key=lambda item: item["key"])
    else:
        items.sort(key=lambda item: (item["value"] is None, -(item["value"] or 0)))

    return {
        "group_by": group_by,
        "metric": metric,
        "items": items,
        "totals": {
            "count": sum(item["count"] for item in items),
            "exceeded_count": sum(item["exceeded_count"] for item in items),
            "anomaly_count": sum(item["anomaly_count"] for item in items),
        },
    }


def _group_key(group_by, data):
    if group_by == "station":
        return data.get("station_code")
    if group_by == "month":
        return "%04d-%02d" % (int(data.get("year")), int(data.get("month")))
    return data.get("bucket") or data.get("area")


def _group_key_label(group_by, data):
    key = _group_key(group_by, data)
    if group_by == "station":
        return key, "%s %s" % (data.get("station_code"), data.get("station_name"))
    if group_by == "area":
        return key, key
    if group_by == "month" or group_by == "day":
        return key, key
    if group_by == "pollutant":
        meta = get_pollutant(key)
        return key, meta["label"] if meta else key
    if group_by == "period":
        return key, PERIOD_LABELS.get(key, key)
    return key, DATA_SOURCE_LABELS.get(key, key)


def option_payload():
    return {
        "group_by": list(GROUP_BY_CHOICES),
        "metric": list(METRIC_CHOICES),
        "sort": list(SORT_CHOICES),
        "exceedance_status": [
            {"value": key, "label": label} for key, label in EXCEEDANCE_STATUS_LABELS.items()
        ],
        "station_type": [
            {"value": key, "label": label} for key, label in STATION_TYPE_LABELS.items()
        ],
    }
