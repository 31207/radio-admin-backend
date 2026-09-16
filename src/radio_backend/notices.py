"""歌曲选用通知缓存（bot 读取发送；后端负责写入与状态查询）。"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from radio_backend.db import SongSelectedNotice, UserRequest, get_session_factory

logger = logging.getLogger("radio_backend.notices")

MAX_ATTEMPTS = 3


def _load_ids(raw: str) -> list[str]:
    try:
        ids = json.loads(raw or "[]")
        return [str(x) for x in ids] if isinstance(ids, list) else []
    except json.JSONDecodeError:
        return []


class NoticeService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._sf = session_factory

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        return self._sf or get_session_factory()

    async def add(self, song_id: int, name: str, artist: str) -> bool:
        """写入一条待通知缓存（含该歌所有点歌用户，去重）；无人点过则跳过。"""
        async with self._factory()() as s:
            uids = list(
                (
                    await s.execute(
                        select(UserRequest.user_id)
                        .where(UserRequest.song_id == song_id)
                        .distinct()
                    )
                ).scalars().all()
            )
        if not uids:
            return False
        async with self._factory()() as s:
            s.add(
                SongSelectedNotice(
                    song_id=song_id,
                    name=name,
                    artist=artist,
                    selected_at=datetime.now(),
                    user_ids=json.dumps(list(dict.fromkeys(uids)), ensure_ascii=False),
                    failed_user_ids="[]",
                    attempts=0,
                    sent=False,
                )
            )
            await s.commit()
            return True

    async def status(self) -> tuple[list[dict], int, list[dict]]:
        """返回 (待发送列表, 已发送数量, 失败列表)。"""
        async with self._factory()() as s:
            rows = (
                await s.execute(
                    select(SongSelectedNotice).order_by(SongSelectedNotice.id.desc())
                )
            ).scalars().all()
        pending: list[dict] = []
        failed: list[dict] = []
        sent_count = 0
        for r in rows:
            item = {
                "id": r.id,
                "name": r.name,
                "artist": r.artist,
                "selected_at": r.selected_at,
                "attempts": r.attempts,
                "failed_user_ids": _load_ids(r.failed_user_ids),
            }
            if not r.sent:
                item["user_ids"] = (
                    _load_ids(r.failed_user_ids) if r.attempts else _load_ids(r.user_ids)
                )
                pending.append(item)
            elif item["failed_user_ids"]:
                failed.append(item)
            else:
                sent_count += 1
        return pending, sent_count, failed
