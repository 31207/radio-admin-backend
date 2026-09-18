"""异步数据库层（SQLAlchemy 2.0 async + asyncpg，PostgreSQL）与 ORM 模型。

连接串来自 radio_backend.config 的 DATABASE_URL（必填）；
engine / session 工厂惰性创建，规避导入顺序问题。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def _pg_url(url: str) -> str:
    """校验并规整 PostgreSQL 连接串（自动补 asyncpg 驱动）。"""
    if not url:
        raise RuntimeError(
            "未配置 DATABASE_URL。请在 backend/.env 中设置，例如：\n"
            "DATABASE_URL=postgresql+asyncpg://user:pass@127.0.0.1:5432/qqbot"
        )
    if not url.startswith("postgresql"):
        raise RuntimeError(f"DATABASE_URL 必须是 PostgreSQL 连接串，当前为：{url}")
    if "+asyncpg" not in url:
        return url.replace("postgresql", "postgresql+asyncpg", 1)
    return url


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        from radio_backend.config import settings

        _engine = create_async_engine(
            _pg_url(settings.database_url), echo=False, pool_pre_ping=True
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(get_engine(), autoflush=False, expire_on_commit=False)
    return _factory


async def init_db() -> None:
    """按模型建表（幂等）。"""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    """进程退出时释放连接池。"""
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None


def _now() -> datetime:
    return datetime.now()


class Base(DeclarativeBase):
    pass


class Song(Base):
    """歌曲库。``selected`` 为 web 后台「选用」标记。"""

    __tablename__ = "songs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, nullable=False, default="")
    source_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    name: Mapped[str] = mapped_column(String, nullable=False, default="")
    artist: Mapped[str] = mapped_column(String, nullable=False, default="")
    album: Mapped[str] = mapped_column(String, nullable=False, default="")
    cover: Mapped[str] = mapped_column(String, nullable=False, default="")
    duration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    url: Mapped[str] = mapped_column(String, nullable=False, default="")
    link: Mapped[str] = mapped_column(String, nullable=False, default="")
    is_banned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    play_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_songs_source_srcid"),)


class User(Base):
    """点歌用户。``is_banned`` 为封禁标记。"""

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class UserRequest(Base):
    """用户点歌记录。同一用户对同一首歌仅一条，重复点歌更新时间（置顶）并累计当日/本周次数。"""

    __tablename__ = "user_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    song_id: Mapped[int] = mapped_column(Integer, ForeignKey("songs.id"), nullable=False)
    time: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    remark: Mapped[str] = mapped_column(String, nullable=False, default="")
    day: Mapped[str] = mapped_column(String, nullable=False, default="")
    day_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    week: Mapped[str] = mapped_column(String, nullable=False, default="")
    week_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("user_id", "song_id", name="uq_ur_user_song"),
        Index("idx_ur_user", "user_id"),
        Index("idx_ur_song", "song_id"),
    )


class PlayHistory(Base):
    """播放/选用历史（web 后台选用即写入一条）。"""

    __tablename__ = "play_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    song_id: Mapped[int] = mapped_column(Integer, ForeignKey("songs.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    note: Mapped[str] = mapped_column(String, nullable=False, default="")
    played_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class SongSelectedNotice(Base):
    """歌曲被选用后的待通知缓存（bot 定时任务读取发送）。"""

    __tablename__ = "song_selected_notice"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    song_id: Mapped[int] = mapped_column(Integer, ForeignKey("songs.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False, default="")
    artist: Mapped[str] = mapped_column(String, nullable=False, default="")
    selected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    user_ids: Mapped[str] = mapped_column(String, nullable=False, default="[]")
    failed_user_ids: Mapped[str] = mapped_column(String, nullable=False, default="[]")
    rejected_user_ids: Mapped[str] = mapped_column(
        String, nullable=False, default="[]", server_default="[]"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
