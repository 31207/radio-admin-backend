"""封面拉取与本地磁盘缓存（经 Go Music API 的封面代理绕过防盗链）。

- 同一 URL 的并发请求在进程内合并为一次下载（in-flight 去重），避免重复请求与写文件竞态。
- 磁盘缓存按 URL 的 md5 命名，永久保留（磁盘换命中率，可手动清空目录）。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path

import httpx
from PIL import Image

_TIMEOUT = 12.0
_in_flight: dict[Path, asyncio.Task] = {}


def cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.jpg"


async def _load_image(data: bytes) -> Image.Image | None:
    def _load() -> Image.Image | None:
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                return img.convert("RGB")
        except Exception:
            return None

    return await asyncio.to_thread(_load)


async def _download(api_base: str, url: str, path: Path, name: str, artist: str) -> Image.Image | None:
    params = {"url": url, "name": name, "artist": artist}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(f"{api_base}/api/v1/music/cover", params=params)
        if resp.status_code != 200 or not resp.content:
            return None
        img = await _load_image(resp.content)
        if img is None:
            return None
        path.write_bytes(resp.content)
        return img
    except httpx.HTTPError:
        return None


async def fetch_cover(
    api_base: str,
    cover_url: str,
    cache_dir: Path,
    name: str = "",
    artist: str = "",
) -> Image.Image | None:
    """取封面：先查本地缓存，未命中则下载（同 URL 并发合并），失败返回 None。"""
    if not cover_url:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(cache_dir, cover_url)

    if path.exists() and path.stat().st_size > 0:
        img = await _load_image(path.read_bytes())
        if img is not None:
            return img
        path.unlink(missing_ok=True)

    task = _in_flight.get(path)
    if task is None:
        task = asyncio.create_task(_download(api_base, cover_url, path, name, artist))
        _in_flight[path] = task
    try:
        return await task
    finally:
        if _in_flight.get(path) is task:
            _in_flight.pop(path, None)
