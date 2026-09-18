"""校园广播站点歌管理台 —— 纯 API 后端（异步 SQLAlchemy + asyncpg，PostgreSQL）。

运行：
    .venv/bin/python app.py    （Windows: .\\.venv\\Scripts\\python.exe）
默认监听：
    http://127.0.0.1:8600（WEB_ADMIN_PORT）

前端（独立项目 radio-admin）通过跨域直接访问本服务，本服务不托管静态文件；
允许的跨域来源由 WEB_ADMIN_CORS 配置（逗号分隔，默认 *，生产环境请收紧）。

配置：使用本项目的 `.env`（复制 `.env.example` 修改；已导出的同名环境变量优先于文件）。

鉴权：设置 WEB_ADMIN_USERNAME / WEB_ADMIN_PASSWORD 后，管理接口需先
`POST /api/login` 拿 token，再带 `Authorization: Bearer <token>`；
两者都不设置则不鉴权（仅本地调试）。

数据库由 DATABASE_URL 决定（必配），与 bot 共用同一数据库。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncIterator

from radio_backend.util import load_dotenv

_ENV_FILE = Path(__file__).resolve().parent / ".env"


def _apply_env_file() -> None:
    """把项目根 .env 导入环境变量（已设置的环境变量优先），须在任何 radio_backend 导入之前执行。"""
    for key, value in load_dotenv(_ENV_FILE).items():
        os.environ.setdefault(key, value)


_apply_env_file()

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import cast, delete, func, select, String

from radio_backend.config import settings
from radio_backend.db import PlayHistory, Song, User, UserRequest, get_session_factory, init_db
from radio_backend.render import render_history_image
from radio_backend.screening import RULES, ScreeningService
from radio_backend.util import beijing_naive_now, today_key

screening = ScreeningService()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await init_db()
    yield


app = FastAPI(title="校园广播站点歌管理后台", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.web_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- 鉴权


def _session_token() -> str:
    if not settings.web_admin_password:
        return ""
    key = settings.web_admin_password.encode()
    msg = settings.web_admin_username.encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def require_auth(authorization: str = Header(default="")) -> None:
    if not settings.web_admin_password and not settings.web_admin_token:
        return
    expect = f"Bearer {_session_token()}" if settings.web_admin_password else f"Bearer {settings.web_admin_token}"
    if not hmac.compare_digest(authorization, expect):
        raise HTTPException(401, "未授权")


AuthDep = Depends(require_auth)
_auth = {"dependencies": [AuthDep]}


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn):
    if not settings.web_admin_password and not settings.web_admin_token:
        return {"ok": True, "token": ""}
    if settings.web_admin_password:
        if body.username == settings.web_admin_username and body.password == settings.web_admin_password:
            return {"ok": True, "token": _session_token()}
        raise HTTPException(401, "账号或密码错误")
    if body.password == settings.web_admin_token:
        return {"ok": True, "token": settings.web_admin_token}
    raise HTTPException(401, "token 错误")


# ---------------------------------------------------------------- 点歌池


@app.get("/api/pool", **_auth)
async def get_pool(name: str = "", user: str = "", status: str = "", page: int = 1, size: int = 20):
    page = max(int(page), 1)
    size = min(max(int(size), 1), 200)
    factory = get_session_factory()

    async with factory() as s:
        sub_agg = (
            select(
                UserRequest.song_id,
                func.count(UserRequest.id).label("req_count"),
                func.max(UserRequest.time).label("last_time"),
            )
            .group_by(UserRequest.song_id)
            .subquery()
        )
        stmt = (
            select(
                Song.id,
                Song.name,
                Song.artist,
                Song.is_banned,
                Song.selected,
                sub_agg.c.req_count,
                sub_agg.c.last_time,
            )
            .join(sub_agg, sub_agg.c.song_id == Song.id)
        )
        conds = []
        if name:
            conds.append(Song.name.like(f"%{name}%"))
        if user:
            conds.append(
                Song.id.in_(
                    select(UserRequest.song_id).where(UserRequest.user_id.like(f"%{user}%"))
                )
            )
        if status == "selected":
            conds.append(Song.selected.is_(True))
        elif status == "banned":
            conds.append(Song.is_banned.is_(True))
        elif status == "pending":
            conds.append(Song.selected.is_(False))
            conds.append(Song.is_banned.is_(False))
        if conds:
            stmt = stmt.where(*conds)

        total = await s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        if not status:
            stmt = stmt.order_by(Song.selected.asc(), sub_agg.c.last_time.desc())
        else:
            stmt = stmt.order_by(sub_agg.c.last_time.desc())
        stmt = stmt.limit(size).offset((page - 1) * size)
        rows = (await s.execute(stmt)).mappings().all()

        song_ids = [r["id"] for r in rows]
        requesters_map: dict[int, list[str]] = {sid: [] for sid in song_ids}
        if song_ids:
            pairs = (
                await s.execute(
                    select(UserRequest.song_id, UserRequest.user_id).where(
                        UserRequest.song_id.in_(song_ids)
                    )
                )
            ).all()
            for sid, uid in pairs:
                if uid not in requesters_map[sid]:
                    requesters_map[sid].append(uid)

        out = [
            {
                "id": r["id"],
                "name": r["name"],
                "artist": r["artist"],
                "is_banned": bool(r["is_banned"]),
                "selected": bool(r["selected"]),
                "req_count": r["req_count"],
                "last_time": r["last_time"],
                "requesters": requesters_map[r["id"]],
            }
            for r in rows
        ]
        return {"data": out, "total": total, "page": page, "size": size}


@app.get("/api/songs/{sid}/requests", **_auth)
async def get_song_requests(sid: int):
    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                select(
                    UserRequest.user_id,
                    UserRequest.time,
                    UserRequest.remark,
                    UserRequest.day_count,
                    Song.name,
                    Song.artist,
                )
                .join(Song, Song.id == UserRequest.song_id)
                .where(UserRequest.song_id == sid)
                .order_by(UserRequest.time.desc())
            )
        ).mappings().all()
        return {"data": [dict(r) for r in rows]}


class SelectManyIn(BaseModel):
    ids: list[int]
    note: str = ""


@app.post("/api/songs/select_many", **_auth)
async def select_many(body: SelectManyIn):
    now = datetime.now()
    factory = get_session_factory()
    async with factory() as s:
        songs = (await s.execute(select(Song).where(Song.id.in_(body.ids)))).scalars().all()
        for song in songs:
            song.selected = True
            s.add(
                PlayHistory(
                    song_id=song.id, user_id="", note=body.note, played_at=now, created_at=now
                )
            )
        await s.commit()
    return {"ok": True, "count": len(songs)}


class SelectIn(BaseModel):
    user_id: str = ""
    note: str = ""


@app.post("/api/songs/{sid}/select", **_auth)
async def select_song(sid: int, body: SelectIn):
    now = datetime.now()
    factory = get_session_factory()
    async with factory() as s:
        song = await s.get(Song, sid)
        if song is None:
            raise HTTPException(404, "歌曲不存在")
        song.selected = True
        s.add(
            PlayHistory(song_id=sid, user_id=body.user_id, note=body.note, played_at=now, created_at=now)
        )
        await s.commit()
    return {"ok": True}


@app.post("/api/songs/{sid}/ban", **_auth)
async def ban_song(sid: int):
    factory = get_session_factory()
    async with factory() as s:
        song = await s.get(Song, sid)
        if song is not None:
            song.is_banned = True
            await s.commit()
    return {"ok": True}


@app.post("/api/songs/{sid}/unban", **_auth)
async def unban_song(sid: int):
    factory = get_session_factory()
    async with factory() as s:
        song = await s.get(Song, sid)
        if song is not None:
            song.is_banned = False
            await s.commit()
    return {"ok": True}


class BanManyIn(BaseModel):
    ids: list[int]


@app.post("/api/songs/ban_many", **_auth)
async def ban_many(body: BanManyIn):
    factory = get_session_factory()
    async with factory() as s:
        res = await s.execute(
            Song.__table__.update().where(Song.id.in_(body.ids)).values(is_banned=True)
        )
        await s.commit()
        return {"ok": True, "count": res.rowcount or 0}


# ---------------------------------------------------------------- 用户


@app.get("/api/users", **_auth)
async def get_users():
    today = today_key()
    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                select(
                    User.user_id,
                    User.is_banned,
                    User.created_at,
                    func.coalesce(
                        func.sum(UserRequest.day_count).filter(UserRequest.day == today), 0
                    ).label("today_count"),
                )
                .outerjoin(UserRequest, UserRequest.user_id == User.user_id)
                .group_by(User.user_id, User.is_banned, User.created_at)
                .order_by(User.user_id)
            )
        ).mappings().all()
        return {
            "data": [
                {
                    "user_id": r["user_id"],
                    "is_banned": bool(r["is_banned"]),
                    "created_at": r["created_at"],
                    "today_count": int(r["today_count"] or 0),
                }
                for r in rows
            ]
        }


@app.post("/api/users/{uid}/ban", **_auth)
async def ban_user(uid: str):
    factory = get_session_factory()
    async with factory() as s:
        user = await s.get(User, uid)
        if user is not None:
            user.is_banned = True
        else:
            s.add(User(user_id=uid, is_banned=True))
        await s.commit()
    return {"ok": True}


@app.post("/api/users/{uid}/unban", **_auth)
async def unban_user(uid: str):
    factory = get_session_factory()
    async with factory() as s:
        user = await s.get(User, uid)
        if user is not None:
            user.is_banned = False
            await s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 播放历史


@app.get("/api/history", **_auth)
async def get_history(name: str = "", date: str = "", page: int = 1, size: int = 20):
    page = max(int(page), 1)
    size = min(max(int(size), 1), 200)
    factory = get_session_factory()
    async with factory() as s:
        stmt = (
            select(
                PlayHistory.id,
                PlayHistory.song_id,
                Song.name,
                Song.artist,
                PlayHistory.user_id,
                PlayHistory.note,
                PlayHistory.played_at,
            )
            .join(Song, Song.id == PlayHistory.song_id)
        )
        conds = []
        if name:
            conds.append(Song.name.like(f"%{name}%"))
        if date:
            conds.append(cast(PlayHistory.played_at, String).like(f"{date}%"))
        if conds:
            stmt = stmt.where(*conds)
        total = await s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        stmt = stmt.order_by(PlayHistory.played_at.desc()).limit(size).offset((page - 1) * size)
        rows = (await s.execute(stmt)).mappings().all()
        return {"data": [dict(r) for r in rows], "total": total, "page": page, "size": size}


async def _reset_songs_by_history_ids(ids: list[int]) -> int:
    if not ids:
        return 0
    factory = get_session_factory()
    async with factory() as s:
        song_ids = list(
            (
                await s.execute(
                    select(PlayHistory.song_id).where(PlayHistory.id.in_(ids)).distinct()
                )
            ).scalars().all()
        )
        await s.execute(delete(PlayHistory).where(PlayHistory.id.in_(ids)))
        if song_ids:
            await s.execute(
                Song.__table__.update().where(Song.id.in_(song_ids)).values(selected=False)
            )
        await s.commit()
    return len(song_ids)


@app.delete("/api/history/{hid}", **_auth)
async def delete_history_one(hid: int):
    factory = get_session_factory()
    async with factory() as s:
        exists = await s.get(PlayHistory, hid) is not None
    if not exists:
        raise HTTPException(404, "播放历史不存在")
    return {"ok": True, "reset_songs": await _reset_songs_by_history_ids([hid])}


class HistoryDeleteManyIn(BaseModel):
    ids: list[int]


@app.post("/api/history/delete_many", **_auth)
async def delete_history_many(body: HistoryDeleteManyIn):
    if not body.ids:
        return {"ok": True, "count": 0, "reset_songs": 0}
    return {
        "ok": True,
        "count": len(body.ids),
        "reset_songs": await _reset_songs_by_history_ids(body.ids),
    }


@app.post("/api/history/delete_all", **_auth)
async def delete_history_all():
    factory = get_session_factory()
    async with factory() as s:
        song_ids = list(
            (await s.execute(select(PlayHistory.song_id).distinct())).scalars().all()
        )
        count = await s.scalar(select(func.count()).select_from(PlayHistory)) or 0
        await s.execute(delete(PlayHistory))
        if song_ids:
            await s.execute(
                Song.__table__.update().where(Song.id.in_(song_ids)).values(selected=False)
            )
        await s.commit()
        return {"ok": True, "count": count, "reset_songs": len(song_ids)}


class HistoryImageIn(BaseModel):
    range: str = "today"  # today / yesterday / week / custom
    date_from: str | None = None
    date_to: str | None = None


def _history_image_range(body: HistoryImageIn) -> tuple[datetime, datetime, str]:
    """按北京时间（与库中无时区时间戳对齐）计算筛选区间与展示文案。"""
    now = beijing_naive_now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if body.range == "today":
        return today0, now, "今天"
    if body.range == "yesterday":
        return today0 - timedelta(days=1), today0, "昨天"
    if body.range == "week":
        return today0 - timedelta(days=today0.weekday()), now, "本周"
    start = datetime.fromisoformat(body.date_from) if body.date_from else today0
    end = (
        datetime.fromisoformat(body.date_to) + timedelta(days=1)
        if body.date_to
        else now
    )
    label = f"{body.date_from or '…'} ~ {body.date_to or '…'}"
    return start, end, label


@app.post("/api/history/image", **_auth)
async def history_image(body: HistoryImageIn):
    start, end, label = _history_image_range(body)
    factory = get_session_factory()
    async with factory() as s:
        records = (
            await s.execute(
                select(
                    Song.id.label("song_id"),
                    Song.name,
                    Song.artist,
                    Song.album,
                    Song.source,
                    PlayHistory.played_at,
                )
                .join(Song, Song.id == PlayHistory.song_id)
                .where(PlayHistory.played_at >= start, PlayHistory.played_at < end)
                .order_by(PlayHistory.played_at.asc())
            )
        ).mappings().all()
        song_ids = {r["song_id"] for r in records}
        counts: dict[int, int] = {}
        if song_ids:
            counts = dict(
                (
                    await s.execute(
                        select(
                            UserRequest.song_id,
                            func.count(func.distinct(UserRequest.user_id)),
                        )
                        .where(UserRequest.song_id.in_(song_ids))
                        .group_by(UserRequest.song_id)
                    )
                ).all()
            )
    if not records:
        raise HTTPException(404, "该时间范围内没有播放记录")

    days_map: dict = {}
    for r in records:
        day = r["played_at"].date()
        songs = days_map.setdefault(day, {})
        if r["song_id"] in songs:
            continue
        songs[r["song_id"]] = {
            "name": r["name"],
            "artist": r["artist"],
            "album": r["album"],
            "source": r["source"],
            "requesters": int(counts.get(r["song_id"], 0)),
        }

    days = [
        {"date": day, "songs": list(songs.values())}
        for day, songs in sorted(days_map.items(), reverse=True)
    ]
    total = sum(len(day["songs"]) for day in days)

    png = await asyncio.to_thread(render_history_image, label, days, total)
    return Response(content=png, media_type="image/png")


# ---------------------------------------------------------------- 每日选曲抽取


class DrawFiltersIn(BaseModel):
    date_from: str | None = None
    date_to: str | None = None
    exclude_selected: bool = True
    exclude_banned: bool = True
    platforms: list[str] = []
    user_ids: list[str] = []
    remark_keyword: str = ""
    count: int = 5
    weighted: bool = False
    exclude_ids: list[int] = []


def _draw_request_conditions(body: DrawFiltersIn):
    """点歌记录侧的筛选条件（时间 / 点歌人 / 备注关键词）。"""
    conds = []
    if body.date_from:
        conds.append(UserRequest.time >= datetime.fromisoformat(body.date_from))
    if body.date_to:
        conds.append(UserRequest.time < datetime.fromisoformat(body.date_to) + timedelta(days=1))
    if body.user_ids:
        conds.append(UserRequest.user_id.in_(body.user_ids))
    if body.remark_keyword:
        conds.append(UserRequest.remark.like(f"%{body.remark_keyword}%"))
    return conds


async def _candidate_song_ids(body: DrawFiltersIn) -> list[int]:
    """按条件得到候选歌曲 id 列表（去重）。"""
    factory = get_session_factory()
    async with factory() as s:
        stmt = (
            select(UserRequest.song_id.distinct())
            .join(Song, Song.id == UserRequest.song_id)
        )
        song_conds = []
        if body.exclude_selected:
            song_conds.append(Song.selected.is_(False))
        if body.exclude_banned:
            song_conds.append(Song.is_banned.is_(False))
        if body.platforms:
            song_conds.append(Song.source.in_(body.platforms))
        if body.exclude_ids:
            song_conds.append(Song.id.notin_(body.exclude_ids))
        if song_conds:
            stmt = stmt.where(*song_conds)
        conds = _draw_request_conditions(body)
        if conds:
            stmt = stmt.where(*conds)
        return list((await s.execute(stmt)).scalars().all())


@app.post("/api/pool/candidates", **_auth)
async def pool_candidates(body: DrawFiltersIn):
    song_ids = await _candidate_song_ids(body)
    users: list[str] = []
    if song_ids:
        factory = get_session_factory()
        async with factory() as s:
            users = list(
                (
                    await s.execute(
                        select(UserRequest.user_id.distinct())
                        .where(UserRequest.song_id.in_(song_ids))
                        .order_by(UserRequest.user_id)
                    )
                ).scalars().all()
            )
    return {"data": {"total": len(song_ids), "users": users}}


def _weighted_sample(items: list[dict], k: int) -> list[dict]:
    """按权重无放回抽样（权重至少为 1）。"""
    pool = list(items)
    weights = [max(int(item["req_count"]), 1) for item in pool]
    chosen: list[dict] = []
    for _ in range(min(k, len(pool))):
        total = sum(weights)
        r = random.uniform(0, total)
        acc = 0.0
        for i, w in enumerate(weights):
            acc += w
            if r <= acc:
                chosen.append(pool[i])
                del pool[i]
                del weights[i]
                break
    return chosen


@app.post("/api/pool/draw", **_auth)
async def pool_draw(body: DrawFiltersIn):
    song_ids = await _candidate_song_ids(body)
    if not song_ids:
        return {"data": {"songs": []}}
    factory = get_session_factory()
    async with factory() as s:
        req_conds = [UserRequest.song_id.in_(song_ids)]
        req_conds.extend(_draw_request_conditions(body))
        counts = {
            sid: cnt
            for sid, cnt in (
                await s.execute(
                    select(UserRequest.song_id, func.count(UserRequest.id))
                    .where(*req_conds)
                    .group_by(UserRequest.song_id)
                )
            ).all()
        }
        remarks: dict[int, str] = {}
        for sid, remark in (
            await s.execute(
                select(UserRequest.song_id, UserRequest.remark).where(
                    UserRequest.song_id.in_(song_ids), UserRequest.remark != ""
                )
            )
        ).all():
            remarks.setdefault(sid, remark)
        requesters: dict[int, list[str]] = {}
        for sid, uid in (
            await s.execute(
                select(UserRequest.song_id, UserRequest.user_id).where(
                    UserRequest.song_id.in_(song_ids)
                )
            )
        ).all():
            if uid not in requesters.setdefault(sid, []):
                requesters[sid].append(uid)
        songs = (await s.execute(select(Song).where(Song.id.in_(song_ids)))).scalars().all()

    candidates = [
        {
            "id": song.id,
            "name": song.name,
            "artist": song.artist,
            "album": song.album,
            "cover": song.cover,
            "source": song.source,
            "url": song.url,
            "link": song.link,
            "req_count": counts.get(song.id, 0),
            "requesters": requesters.get(song.id, []),
            "remark": remarks.get(song.id, ""),
        }
        for song in songs
    ]
    count = min(max(int(body.count), 1), len(candidates))
    picked = _weighted_sample(candidates, count) if body.weighted else random.sample(candidates, count)
    return {"data": {"songs": picked}}


# ---------------------------------------------------------------- 筛选 agent


@app.get("/api/agent/rules", **_auth)
async def agent_rules():
    return {"rules": RULES}


class ScreenSongsIn(BaseModel):
    song_ids: list[int]


@app.post("/api/agent/screen-songs", **_auth)
async def screen_songs(body: ScreenSongsIn):
    return {"data": await screening.screen(body.song_ids)}


# ---------------------------------------------------------------- 统计


@app.get("/api/stats", **_auth)
async def get_stats():
    factory = get_session_factory()
    async with factory() as s:
        total = await s.scalar(select(func.count()).select_from(UserRequest)) or 0
        requests = (
            await s.scalar(select(func.count(func.distinct(UserRequest.song_id)))) or 0
        )
        selected = (
            await s.scalar(select(func.count()).select_from(Song).where(Song.selected.is_(True))) or 0
        )
        banned = (
            await s.scalar(select(func.count()).select_from(Song).where(Song.is_banned.is_(True))) or 0
        )
        pending = (
            await s.scalar(
                select(func.count())
                .select_from(Song)
                .where(Song.selected.is_(False))
                .where(Song.is_banned.is_(False))
                .where(Song.id.in_(select(UserRequest.song_id).distinct()))
            )
            or 0
        )
        hot = [
            dict(r)
            for r in (
                await s.execute(
                    select(Song.name, Song.artist, func.count(UserRequest.id).label("cnt"))
                    .join(UserRequest, UserRequest.song_id == Song.id)
                    .group_by(Song.id)
                    .order_by(func.count(UserRequest.id).desc())
                    .limit(10)
                )
            ).mappings().all()
        ]
        trend = [
            {"day": r[0], "cnt": r[1]}
            for r in (
                await s.execute(
                    select(UserRequest.day, func.count()).group_by(UserRequest.day).order_by(UserRequest.day)
                )
            ).all()
        ]
        return {
            "data": {
                "total": total,
                "requests": requests,
                "pending": pending,
                "selected": selected,
                "banned": banned,
                "hot": hot,
                "trend": trend,
            }
        }


# ---------------------------------------------------------------- 权限白名单


def _read_permissions() -> dict:
    try:
        return json.loads(settings.permissions_file.read_text(encoding="utf-8"))
    except Exception:
        return {"admins": [], "super_admins": []}


@app.get("/api/permissions", **_auth)
async def get_permissions():
    return {"data": _read_permissions()}


class PermissionsIn(BaseModel):
    admins: list[str]
    super_admins: list[str]


@app.put("/api/permissions", **_auth)
async def put_permissions(body: PermissionsIn):
    data = {"admins": [str(x) for x in body.admins], "super_admins": [str(x) for x in body.super_admins]}
    try:
        settings.permissions_file.parent.mkdir(parents=True, exist_ok=True)
        settings.permissions_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"ok": True}
    except Exception as exc:  # pragma: no cover
        raise HTTPException(500, f"写入白名单失败: {exc}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=settings.web_admin_port)
