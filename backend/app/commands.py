"""Flask CLI commands: flask init-db / seed / reset-db / scan-anomalies."""
import click

from .extensions import db
from .models import Exceedance, Measurement, Station


def register_commands(app):
    @app.cli.command("init-db")
    def init_db():
        """Create database tables."""
        db.create_all()
        click.echo("数据库表已创建")

    @app.cli.command("seed")
    @click.option("--days", default=5, show_default=True, help="生成最近多少天的数据")
    @click.option("--force", is_flag=True, help="已有数据时仍然追加写入")
    def seed(days, force):
        """Load demo stations and monitoring records."""
        from .seed import seed_demo_data

        if Station.query.count() and not force:
            click.echo("已存在监测点数据, 如确需追加请使用 --force")
            return
        db.create_all()
        totals = seed_demo_data(days=days)
        click.echo(
            "演示数据写入完成: 监测点 %(stations)s 个, 监测数据 %(measurements)s 条, "
            "超标记录 %(exceedances)s 条" % totals
        )

    @app.cli.command("reset-db")
    @click.option("--with-demo/--empty", default=True, help="是否写入演示数据")
    def reset_db(with_demo):
        """Drop all tables, recreate them and optionally load demo data."""
        from .seed import reset_database, seed_demo_data

        reset_database()
        click.echo("数据库已重置")
        if with_demo:
            totals = seed_demo_data()
            click.echo("演示数据写入完成: %s" % totals)

    @app.cli.command("stats")
    def stats():
        """Print a short record summary."""
        click.echo(
            "监测点 %d 个 / 监测数据 %d 条 / 超标记录 %d 条"
            % (
                Station.query.count(),
                Measurement.query.count(),
                Exceedance.query.count(),
            )
        )

    @app.cli.command("scan-anomalies")
    @click.option("--limit", default=20, show_default=True, help="最多列出多少条异常记录")
    def scan_anomalies(limit):
        """识别服务端校验上线前混入的历史异常监测值 (负数 / 明显不合理极大值)。

        这些记录不会计入达标率与排名, 可在数据查询页用 anomaly=only 筛选后
        删除或以正确数值覆盖重提, 修正后统计会按同一口径自动重算。
        """
        from .domain.value_validation import is_anomalous_value
        from .services import query_service

        filters = query_service.parse_filters({})
        summary = query_service.summary(filters)
        rows = (
            query_service.apply_filters(
                db.session.query(Measurement), {**filters, "anomaly": "only"}
            )
            .order_by(Measurement.id.asc())
            .all()
        )
        linked_exceedances = Exceedance.query.filter(
            Exceedance.measurement_id.in_([row.id for row in rows])
        ).count() if rows else 0
        click.echo("共识别异常监测数据 %d 条 (其中关联超标记录 %d 条)" % (len(rows), linked_exceedances))
        click.echo(
            "有效数据 %d 条, 达标率 %s / 超标率 %s (均已排除异常值)"
            % (
                summary["total"],
                _pct(summary["compliance_rate"]),
                _pct(summary["exceed_rate"]),
            )
        )
        for row in rows[:limit]:
            label = is_anomalous_value and row.pollutant_label()
            click.echo(
                "  #%d  %s %s  %s=%s %s  录入人=%s"
                % (
                    row.id,
                    row.measured_at.strftime("%Y-%m-%d %H:%M"),
                    row.station.code if row.station else "?",
                    label,
                    row.value,
                    row.unit or "",
                    row.recorder or "-",
                )
            )
        if len(rows) > limit:
            click.echo("  ... 其余 %d 条请在查询页以\"仅看异常值\"筛选" % (len(rows) - limit))


def _pct(value):
    return "-" if value is None else "%.2f%%" % (value * 100)
