"""Shared fixtures. Concurrency guarantees are verified against a real
PostgreSQL (row locks / FOR UPDATE have no SQLite equivalent), so tests
skip cleanly when DATABASE_URL is unreachable.
"""

from __future__ import annotations

import os

# Default to the portable local instance used in development/CI.
_DEFAULT_DSN = "postgresql+psycopg2://seatbond:seatbond@127.0.0.1:55444/seatbond"
os.environ.setdefault("DATABASE_URL", _DEFAULT_DSN)

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.services import hold_service  # noqa: E402
from app.services.schema_upgrade import ensure_columns  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("PostgreSQL not reachable; skipping DB-backed tests", allow_module_level=False)
    Base.metadata.create_all(bind=engine)
    ensure_columns(engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_db():
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE conflict_logs, seat_holds, showtimes, halls "
                "RESTART IDENTITY CASCADE"
            )
        )
    hold_service.pre_lock_hook = None
    yield
    hold_service.pre_lock_hook = None
