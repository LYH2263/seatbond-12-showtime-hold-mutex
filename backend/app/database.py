from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config import settings

# 临界区会话的执行选项标记（仅 SQLite 使用）
IMMEDIATE_OPT = "seatbond_immediate"


def _configure_sqlite(engine: Engine) -> None:
    """让 SQLite 的写事务具备与 Postgres 行锁等价的串行能力。

    生产使用 Postgres（SELECT ... FOR UPDATE 让同场次并发请求在行锁上排队）。
    SQLite 无行锁，测试时通过 begin 事件让标记为临界区的事务以
    BEGIN IMMEDIATE 开启：首条语句即取得库级 RESERVED 写锁，第二个写事务
    阻塞到先到者提交后才开始，因此后到者在同一事务里读到的必是最新持座。
    普通会话仍为 DEFERRED，只用于短的快照读，避免读事务长期占 SHARED 锁。
    """

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()
        # 关闭 sqlite3 驱动隐式的延迟 BEGIN，交由下面的 begin 事件控制
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _sqlite_begin(conn):
        immediate = conn._execution_options.get(IMMEDIATE_OPT, False)
        conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")


def build_engine(url: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        _configure_sqlite(engine)
    return engine


engine = build_engine(settings.database_url)
# Postgres 下两种会话共用引擎（由 FOR UPDATE 串行）；SQLite 下临界区引擎的
# 事务以 BEGIN IMMEDIATE 开启
immediate_engine = (
    engine.execution_options(**{IMMEDIATE_OPT: True})
    if engine.dialect.name == "sqlite"
    else engine
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
ImmediateSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=immediate_engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """普通请求会话：Postgres 读已提交 / SQLite DEFERRED 短快照。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_immediate_db():
    """临界区写会话：SQLite 下以 BEGIN IMMEDIATE 开启，Postgres 下配合 FOR UPDATE。"""
    db = ImmediateSessionLocal()
    try:
        yield db
    finally:
        db.close()
