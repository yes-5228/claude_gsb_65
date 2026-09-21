"""Flask CLI commands: flask init-db / seed / reset-db."""
import click

from .extensions import db
from .models import Exceedance, Measurement, Station


def register_commands(app):
    @app.cli.command("init-db")
    def init_db():
        """Create database tables."""
        from .migrations import ensure_schema

        db.create_all()
        ensure_schema()
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

    @app.cli.command("quality-scan")
    @click.option("--rescan-all", is_flag=True, help="连同已标记的记录一起重新判定")
    def quality_scan(rescan_all):
        """Scan history for implausible values (negative / out of range) and flag them."""
        from .migrations import ensure_schema
        from .services import quality_service

        ensure_schema()
        result = quality_service.scan_invalid_measurements(rescan_all=rescan_all)
        click.echo(
            "质量扫描完成: 扫描 %(scanned)d 条, 新标记异常 %(flagged)d 条, "
            "此前已标记 %(already_flagged)d 条, 恢复有效 %(restored)d 条" % result
        )
