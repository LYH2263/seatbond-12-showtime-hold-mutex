"""Lightweight idempotent schema upgrades.

The project creates tables with ``Base.metadata.create_all`` (no
Alembic). Rows added to models after a volume was first provisioned are
backfilled here on startup so an existing Postgres volume keeps working.
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# table -> {column: DDL type}
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "conflict_logs": {
        "kind": "VARCHAR(20) NOT NULL DEFAULT 'overlap'",
        "row": "INTEGER",
        "start_col": "INTEGER",
        "end_col": "INTEGER",
    },
}


def ensure_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all will build it with all current columns
            present = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
