"""监测数据查询: 过滤条件解析, 统计聚合与导出数据准备."""
from datetime import datetime, time

from sqlalchemy import case, cast, func, or_

from ..domain.constants import (
    DATA_SOURCE_LABELS,
    EXCEEDANCE_STATUS_LABELS,
    PERIOD_LABELS,
    STATION_TYPE_LABELS,
)
from ..domain.standards import POLLUTANT_CODES, get_pollutant
from ..errors import ValidationError
from ..extensions import db
from ..models import Exceedance, Measurement, Station
from ..models.base import iso
from ..utils.validation import parse_date

GROUP_BY_CHOICES = ("station", "area", "pollutant", "period", "day", "month", "data_source")
METRIC_CHOICES = ("avg", "max", "min", "count", "sum")
SORT_CHOICES = ("measured_at", "value", "exceed_ratio", "pollutant", "station_code", "created_at")


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
        # quality: all(默认, 列表用) / valid / invalid
        "quality": (args.get("quality") or "all").strip(),
        "exceedance_status": _split(args.get("exceedance_status")),
        "date_from": _date_arg(args, "date_from"),
        "date_to": _date_arg(args, "date_to", end_of_day=True),
        "min_value": _float_arg(args, "min_value"),
        "max_value": _float_arg(args, "max_value"),
        "keyword": (args.get("keyword") or "").strip(),
        "recorder": (args.get("recorder") or "").strip(),
    }
    if filters["quality"] not in ("all", "valid", "invalid"):
        raise ValidationError(
            "quality 仅支持: all, valid, invalid", fields={"quality": "unknown"}
        )
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
    if filters.get("quality") == "valid":
        query = query.filter(Measurement.is_valid.is_(True))
    elif filters.get("quality") == "invalid":
        query = query.filter(Measurement.is_valid.is_(False))
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

    达标率 / 均值等统计量只统计通过合理性闸门的有效数据 (``is_valid=True``);
    历史混入的负值、超量程极大值通过 ``invalid_count`` 单独说明, 不进分子分母。
    ``total`` 仍为筛选范围内的全部记录数 (含异常), 与列表分页口径一致。
    """
    query = apply_filters(
        db.session.query(
            func.count(Measurement.id),
            func.sum(cast(Measurement.is_exceeded, db.Integer)),
            func.count(func.distinct(Measurement.station_id)),
            func.min(Measurement.measured_at),
            func.max(Measurement.measured_at),
            func.avg(Measurement.value),
            func.sum(cast(Measurement.is_valid, db.Integer)),
        ),
        filters,
    )
    total, exceeded, stations, first_at, last_at, avg_value, valid_count = query.one()
    total = int(total or 0)
    exceeded = int(exceeded or 0)
    valid_count = int(valid_count or 0)
    invalid_count = total - valid_count

    # 有效数据口径下的达标率 (1 - 超标率); 异常记录不参与。
    valid_exceeded = int(
        apply_filters(
            db.session.query(func.sum(cast(Measurement.is_exceeded, db.Integer))),
            {**filters, "quality": "valid"},
        ).scalar()
        or 0
    )
    valid_avg = (
        apply_filters(
            db.session.query(func.avg(Measurement.value)), {**filters, "quality": "valid"}
        ).scalar()
    )
    return {
        "total": total,
        "valid_count": valid_count,
        "invalid_count": invalid_count,
        "exceeded_count": exceeded,
        "valid_exceeded_count": valid_exceeded,
        "compliance_rate": round(1 - valid_exceeded / valid_count, 4) if valid_count else 0.0,
        "exceed_rate": round(valid_exceeded / valid_count, 4) if valid_count else 0.0,
        "station_count": int(stations or 0),
        "first_measured_at": iso(first_at),
        "last_measured_at": iso(last_at),
        "avg_value": round(float(valid_avg), 2) if valid_avg is not None else None,
    }


def statistics(args):
    """Grouped aggregation used by the query page statistics panel."""
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

    # 聚合只取有效数据: case 过滤使无效值返回 NULL, 聚合函数自动忽略 NULL;
    # count/sum 用 else_=0 保证有效条数不被异常数据放大。
    valid_value = case((Measurement.is_valid.is_(True), Measurement.value))
    metric_expr = {
        "avg": func.avg(valid_value),
        "max": func.max(valid_value),
        "min": func.min(valid_value),
        "count": func.sum(case((Measurement.is_valid.is_(True), 1), else_=0)),
        "sum": func.sum(valid_value),
    }[metric].label("metric_value")
    valid_count_expr = func.sum(cast(Measurement.is_valid, db.Integer)).label("valid_count")
    invalid_count_expr = func.sum(
        case((Measurement.is_valid.is_(False), 1), else_=0)
    ).label("invalid_count")
    exceeded_expr = func.sum(
        case(
            (db.and_(Measurement.is_valid.is_(True), Measurement.is_exceeded.is_(True)), 1),
            else_=0,
        )
    ).label("exceeded_count")
    aggregate_columns = (metric_expr, valid_count_expr, invalid_count_expr, exceeded_expr)

    if group_by == "station":
        query = db.session.query(
            Station.id.label("station_id"),
            Station.code.label("station_code"),
            Station.name.label("station_name"),
            Station.area.label("area"),
            *aggregate_columns,
        ).group_by(Station.id, Station.code, Station.name, Station.area)
        is_time_group = False
    elif group_by == "area":
        query = db.session.query(Station.area.label("area"), *aggregate_columns).group_by(
            Station.area
        )
        is_time_group = False
    elif group_by == "day":
        bucket = func.date(Measurement.measured_at).label("bucket")
        query = db.session.query(bucket, *aggregate_columns).group_by(bucket)
        is_time_group = True
    elif group_by == "month":
        year = func.extract("year", Measurement.measured_at).label("year")
        month = func.extract("month", Measurement.measured_at).label("month")
        query = db.session.query(year, month, *aggregate_columns).group_by(year, month)
        is_time_group = True
    else:
        column = {
            "pollutant": Measurement.pollutant,
            "period": Measurement.period,
            "data_source": Measurement.data_source,
        }[group_by]
        query = db.session.query(column.label("bucket"), *aggregate_columns).group_by(column)
        is_time_group = False

    query = apply_filters(query, filters)
    rows = query.all()

    items = []
    for row in rows:
        data = dict(row._mapping)
        valid_count = int(data.get("valid_count") or 0)
        invalid_count = int(data.get("invalid_count") or 0)
        exceeded = int(data.get("exceeded_count") or 0)
        raw_value = data.get("metric_value")
        if group_by == "station":
            key = data.get("station_code")
            label = "%s %s" % (data.get("station_code"), data.get("station_name"))
        elif group_by == "area":
            key = label = data.get("area")
        elif group_by == "day":
            key = str(data.get("bucket"))
            label = key
        elif group_by == "month":
            key = "%04d-%02d" % (int(data.get("year")), int(data.get("month")))
            label = key
        elif group_by == "pollutant":
            key = data.get("bucket")
            meta = get_pollutant(key)
            label = meta["label"] if meta else key
        elif group_by == "period":
            key = data.get("bucket")
            label = PERIOD_LABELS.get(key, key)
        else:
            key = data.get("bucket")
            label = DATA_SOURCE_LABELS.get(key, key)

        items.append(
            {
                "key": key,
                "label": label,
                "value": round(float(raw_value), 2) if raw_value is not None else None,
                "count": valid_count,
                "invalid_count": invalid_count,
                "exceeded_count": exceeded,
                "exceed_rate": round(exceeded / valid_count, 4) if valid_count else 0.0,
                "compliance_rate": (
                    round(1 - exceeded / valid_count, 4) if valid_count else 0.0
                ),
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
            "invalid_count": sum(item["invalid_count"] for item in items),
            "exceeded_count": sum(item["exceeded_count"] for item in items),
        },
    }


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
