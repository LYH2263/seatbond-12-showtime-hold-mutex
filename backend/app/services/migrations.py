"""Lightweight idempotent schema upgrades for deployments without a migration tool.

`Base.metadata.create_all` creates missing tables but never adds columns to an
existing table. New deployments are already complete; this only patches older
databases by adding nullable columns when absent.
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# column name -> DDL type
_CONFLICT_ADDED_COLUMNS = {
    "request_code": "VARCHAR(40)",
    "requested_row": "INTEGER",
    "requested_start_col": "INTEGER",
    "requested_end_col": "INTEGER",
    "blocking_order_code": "VARCHAR(40)",
}


def ensure_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    if "conflict_logs" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("conflict_logs")}
    with engine.begin() as conn:
        for name, ddl_type in _CONFLICT_ADDED_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE conflict_logs ADD COLUMN {name} {ddl_type}"))
