"""歌曲禁播判定 agent（LLM，后端执行，key 不暴露给前端）。

- 逐首调用 OpenAI 兼容大模型，判定结果分三类：安全 / 可疑 / 禁播，附一句理由；
- 结果带 TTL 缓存（同一首歌短时间内不重复判定），并发上限 3；
- LLM 未配置或失败时返回 unknown（前端展示，不阻塞流程）。
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from radio_backend.config import settings
from radio_backend.db import Song, get_session_factory

logger = logging.getLogger("radio_backend.screening")

CACHE_TTL = 24 * 3600
CACHE_MAX = 500
MAX_CONCURRENCY = 3

RULES = """禁止播放类型（判定为「禁播」）：
1. 纯音乐 / 伴奏 / 无人声曲目（广播站需要有人声演唱的歌曲）
2. 重金属 / 死亡金属等极端金属乐
3. 歌词包含脏话、低俗、暴力、歧视、宗教极端等不宜校园广播的内容
4. 广告曲 / 推广曲 / 明显商业推销歌曲
5. 劣质翻唱 / 明显恶搞改编曲

可疑类型（判定为「可疑」）：
1. 电音 / 喊麦 / 洗脑神曲等风格可能不合适的歌曲
2. 信息不足、无法确认的歌曲

其余正常流行歌曲判定为「安全」。"""

_VERDICT_MAP = {"安全": "safe", "可疑": "suspicious", "禁播": "banned"}


class ScreeningService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._sf = session_factory
        self._cache: dict[int, tuple[float, str, str]] = {}
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self._client = None
        if settings.llm_api_base and settings.llm_api_key:
            try:
                from openai import AsyncOpenAI

                self._client = AsyncOpenAI(
                    api_key=settings.llm_api_key,
                    base_url=settings.llm_api_base,
                    timeout=settings.llm_timeout,
                )
            except ImportError:
                logger.error("未安装 openai 库，筛选 agent 不可用")

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        return self._sf or get_session_factory()

    async def screen(self, song_ids: list[int]) -> list[dict]:
        """逐首判定，返回 [{song_id, name, artist, verdict, reason}]。"""
        if not song_ids:
            return []
        songs = await self._load_songs(song_ids)
        results: list[dict] = []
        for song in songs:
            verdict, reason = await self._judge_cached(song)
            results.append(
                {
                    "song_id": song.id,
                    "name": song.name,
                    "artist": song.artist,
                    "verdict": verdict,
                    "reason": reason,
                }
            )
        return results

    async def _load_songs(self, song_ids: list[int]) -> list[Song]:
        async with self._factory()() as s:
            rows = (
                await s.execute(select(Song).where(Song.id.in_(song_ids)))
            ).scalars().all()
            return sorted(rows, key=lambda r: song_ids.index(r.id)) if rows else []

    async def _judge_cached(self, song: Song) -> tuple[str, str]:
        cached = self._cache.get(song.id)
        if cached and time.time() - cached[0] < CACHE_TTL:
            return cached[1], cached[2]
        async with self._sem:
            verdict, reason = await self._judge(song)
        self._cache[song.id] = (time.time(), verdict, reason)
        if len(self._cache) > CACHE_MAX:
            for key in list(self._cache)[: len(self._cache) - CACHE_MAX]:
                self._cache.pop(key, None)
        return verdict, reason

    async def _judge(self, song: Song) -> tuple[str, str]:
        if self._client is None:
            return "unknown", "未配置 LLM（LLM_API_BASE / LLM_API_KEY），无法判定"
        info = (
            f"歌名：{song.name}\n歌手：{song.artist}\n"
            f"专辑：{song.album or '未知'}\n来源平台：{song.source or '未知'}\n"
            f"时长：{song.duration} 秒"
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是校园广播站的歌曲审查助手。请结合你的知识判断下面这首歌是否属于禁止播放类型。\n\n"
                    f"判定规则：\n{RULES}\n\n"
                    "只输出一行，格式严格为：判定|理由（判定只能是：安全、可疑、禁播；理由一句话，中文）"
                ),
            },
            {"role": "user", "content": info},
        ]
        try:
            resp = await self._client.chat.completions.create(
                model=settings.llm_model, messages=messages
            )
            text = (resp.choices[0].message.content or "").strip()
        except Exception:
            logger.exception("筛选 agent 调用失败：%s - %s", song.name, song.artist)
            return "unknown", "判定服务调用失败，请稍后重试"
        if "|" in text:
            label, _, reason = text.partition("|")
            verdict = _VERDICT_MAP.get(label.strip())
            if verdict:
                return verdict, reason.strip() or "（无理由）"
        for label, verdict in _VERDICT_MAP.items():
            if label in text:
                return verdict, text.replace(label, "").strip("，。| ") or "（无理由）"
        return "unknown", text[:80] or "模型未给出有效判定"
