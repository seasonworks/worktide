import logging
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)

_IS_SQLITE = settings.database_url.startswith("sqlite")

# SQLite 在多线程场景下需要关闭 same-thread 检查；其他数据库忽略该参数
connect_args = {"check_same_thread": False} if _IS_SQLITE else {}

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    future=True,
)


if _IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):  # noqa: ANN001
        """每个 SQLite 连接建立时设置 PRAGMA：
        - WAL：读写不互斥，避免查询/上报/清理相互锁表
        - synchronous=NORMAL：配合 WAL 降低 fsync 开销
        - busy_timeout：遇锁等待而非立即报 "database is locked"
          30s：多 agent 并发上报 + 清理大删时，5s 在重启惊群下不够（曾日均
          ~127 次 "database is locked" 500），放宽到 30s 让写入排队而非失败
        - journal_size_limit：checkpoint 后把 WAL 文件截回 ≤64MB，根治高水位膨胀
          （PASSIVE checkpoint 只复用空间不缩文件，曾涨到 285MB）
        - foreign_keys=ON：使 ondelete CASCADE 生效
        """
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_size_limit=67108864")  # 64 MiB
            cursor.execute("PRAGMA foreign_keys=ON")
            logger.debug(
                "SQLite PRAGMA 已设置"
                "（WAL/synchronous/busy_timeout/journal_size_limit/foreign_keys）"
            )
        finally:
            cursor.close()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每个请求一个数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def checkpoint_truncate() -> None:
    """强制一次 WAL 全量 checkpoint 并把 WAL 文件截到 0。

    配合 journal_size_limit：日常由清理线程每天调用一次（见 cleanup.py），
    把累积的 WAL 落库并回收磁盘，防止 WAL 高水位长期占盘。非 SQLite 直接跳过。
    返回的 (busy, log, checkpointed) 仅记日志，失败不抛（清理线程不应因此中断）。
    """
    if not _IS_SQLITE:
        return
    try:
        with engine.connect() as conn:
            row = conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        logger.info("WAL checkpoint(TRUNCATE) 完成：%s", tuple(row) if row else None)
    except Exception:  # noqa: BLE001  checkpoint 失败不影响主服务
        logger.exception("WAL checkpoint(TRUNCATE) 失败")


def _migrate_employee_archive_columns(engine_) -> None:
    """启动期幂等 schema 迁移：补齐 employees.is_active / deleted_at。

    SQLAlchemy create_all 只补缺失的表、不补新列；此处用 SQLite 原生 ALTER TABLE
    幂等补齐，避免引入 Alembic。仅 SQLite 生效；迁到 PostgreSQL 时另行处理。
    首启表不存在时跳过（create_all 会按当前 models 直接建表带新列）。
    """
    if not _IS_SQLITE:
        return
    with engine_.connect() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='employees'"
            )
        }
        if "employees" not in tables:
            return
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(employees)")}
        altered = False
        if "is_active" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE employees ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1"
            )
            altered = True
        if "deleted_at" not in cols:
            conn.exec_driver_sql("ALTER TABLE employees ADD COLUMN deleted_at DATETIME")
            altered = True
        if altered:
            conn.commit()
            logger.info("employees 表已补齐归档列（is_active / deleted_at）")


def _migrate_employee_identity_columns(engine_) -> None:
    """Phase 6.0D · 启动期幂等 schema 迁移：补齐 Device Identity Hardening 5 列。

    与 _migrate_employee_archive_columns 同一模式：仅 SQLite 生效；首启表不存在
    时跳过（create_all 会按当前 models 直接建表带新列）；已有列时跳过。

    新增列：
        hw_fingerprint              TEXT NULL
        hw_fingerprint_first_seen   DATETIME NULL
        legacy_machine_id           TEXT NULL
        clone_suspect_count         INTEGER NOT NULL DEFAULT 0
        clone_suspect_last_at       DATETIME NULL
    """
    if not _IS_SQLITE:
        return
    with engine_.connect() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='employees'"
            )
        }
        if "employees" not in tables:
            return
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(employees)")}
        added: list[str] = []
        # 字段顺序与 models.Employee 一致；NOT NULL 列必带 DEFAULT，避免 ALTER 失败
        if "hw_fingerprint" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE employees ADD COLUMN hw_fingerprint TEXT"
            )
            added.append("hw_fingerprint")
        if "hw_fingerprint_first_seen" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE employees ADD COLUMN hw_fingerprint_first_seen DATETIME"
            )
            added.append("hw_fingerprint_first_seen")
        if "legacy_machine_id" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE employees ADD COLUMN legacy_machine_id TEXT"
            )
            added.append("legacy_machine_id")
        if "clone_suspect_count" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE employees ADD COLUMN "
                "clone_suspect_count INTEGER NOT NULL DEFAULT 0"
            )
            added.append("clone_suspect_count")
        if "clone_suspect_last_at" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE employees ADD COLUMN clone_suspect_last_at DATETIME"
            )
            added.append("clone_suspect_last_at")
        if added:
            conn.commit()
            logger.info(
                "Phase 6.0D · employees 表已补齐设备身份列：%s",
                ", ".join(added),
            )


# 模块加载时执行一次：早于 Base.metadata.create_all 运行，保证旧表也升级到位
_migrate_employee_archive_columns(engine)
_migrate_employee_identity_columns(engine)
