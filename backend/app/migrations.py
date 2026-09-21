"""Minimal additive schema migrations for deployments without Alembic.

The project creates tables with ``db.create_all()``, which never alters an
existing table. These helpers add columns introduced after first release,
keeping existing SQLite / PostgreSQL databases working without a reset.
"""
from sqlalchemy import inspect, text

from .extensions import db

# table -> {column: column DDL fragment}
ADDITIVE_COLUMNS = {
    "measurements": {
        "is_valid": "BOOLEAN NOT NULL DEFAULT 1",
        "invalid_reason": "VARCHAR(255)",
    },
}


def _existing_columns(table):
    inspector = inspect(db.engine)
    if table not in inspector.get_table_names():
        return None
    return {column["name"] for column in inspector.get_columns(table)}


def ensure_schema():
    """Add missing additive columns; create nothing else (tables via create_all).

    Returns True when a data-quality scan should run afterwards
    (i.e. the quality columns were newly added to an existing table).
    """
    should_scan = False
    for table, columns in ADDITIVE_COLUMNS.items():
        present = _existing_columns(table)
        if present is None:
            continue
        for name, ddl in columns.items():
            if name not in present:
                db.session.execute(text("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl)))
                if name == "is_valid":
                    should_scan = True
        db.session.commit()
    return should_scan
