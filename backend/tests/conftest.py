import os
import tempfile

# 必须在导入 app 之前指向文件型 SQLite（WAL 支持多线程并发），并关闭启动种子
_TMP = tempfile.mkdtemp(prefix="seatbond-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/seatbond.db"
os.environ["SEED_ON_EMPTY"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.models import Hall, Showtime  # noqa: E402
from sqlalchemy import select  # noqa: E402


@pytest.fixture()
def client():
    # 每个用例一套干净的库结构
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def make_showtime():
    def _make(*, rows=3, cols=10, aisle_cols=""):
        db = SessionLocal()
        try:
            hall = Hall(name=f"厅-{len(db.scalars(select(Hall)).all()) + 1}", rows=rows,
                        cols=cols, aisle_cols=aisle_cols)
            db.add(hall)
            db.flush()
            from datetime import datetime, timedelta
            st = Showtime(hall_id=hall.id, film_title="并发测试片",
                          start_at=datetime.utcnow() + timedelta(hours=1))
            db.add(st)
            db.commit()
            return st.id, hall.id
        finally:
            db.close()
    return _make
