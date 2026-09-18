"""播放历史卡片长图渲染（Pillow，同步函数，调用方用 asyncio.to_thread 包装）。"""

from __future__ import annotations

import io
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("radio_backend.render")

WIDTH = 920
PAD = 28
HEADER_H = 118
FOOTER_H = 52

DAY_HEADER_H = 52
SONG_ROW_H = 64
DAY_GAP = 18

BG = (245, 246, 248)
ROW_BG = (255, 255, 255)
DIVIDER = (229, 231, 236)
TEXT_MAIN = (33, 37, 43)
TEXT_SUB = (128, 134, 143)
ACCENT = (76, 141, 255)

SOURCE_NAMES = {
    "netease": "网易云",
    "qq": "QQ音乐",
    "kugou": "酷狗",
    "kuwo": "酷我",
    "migu": "咪咕",
    "joox": "JOOX",
    "soda": "汽水",
    "jamendo": "Jamendo",
    "qianqian": "千千",
    "bilibili": "B站",
    "fivesing": "5sing",
}

SOURCE_COLORS = {
    "netease": (227, 59, 59),
    "qq": (49, 194, 124),
    "kugou": (44, 166, 248),
    "kuwo": (255, 126, 5),
    "migu": (255, 62, 77),
    "soda": (58, 130, 246),
}

_WEEK_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

_REGULAR_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-DemiLight.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-DemiLight.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/Deng.ttf",
)
_BOLD_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Medium.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)

_CJK_NAME_KEYWORDS = (
    "cjk", "uming", "ukai", "wqy", "microhei", "zenhei",
    "sourcehansans", "notosanssc", "notoserifsc", "msyh", "simhei", "simsun", "deng",
)
_BOLD_NAME_KEYWORDS = ("bold", "medium", "black", "semibold")


def _fc_match_file(pattern: str) -> str | None:
    """用 fontconfig 按 pattern 查询字体文件路径；不可用返回 None。"""
    try:
        out = subprocess.run(
            ["fc-match", "-f", "%{file}", pattern],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    path = (out.stdout or "").strip().splitlines()[:1]
    return path[0].strip() if path and path[0].strip() else None


def _scan_cjk_fonts() -> list[Path]:
    """扫描常见字体目录下的 CJK 字体（fontconfig 不可用时兜底）。"""
    roots = (
        Path("/usr/share/fonts"),
        Path.home() / ".fonts",
        Path.home() / ".local/share/fonts",
    )
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in (".ttf", ".ttc", ".otf"):
                continue
            if any(k in path.name.lower() for k in _CJK_NAME_KEYWORDS):
                found.append(path)
    return sorted(set(found))


def _resolve_fonts() -> tuple[str | None, str | None]:
    """解析常规/加粗 CJK 字体：显式候选 → fontconfig → 目录扫描。"""
    regular = next((p for p in _REGULAR_CANDIDATES if os.path.exists(p)), None)
    bold = next((p for p in _BOLD_CANDIDATES if os.path.exists(p)), None)

    if regular is None:
        for pattern in ("sans-serif:lang=zh-cn", "sans-serif:lang=zh"):
            candidate = _fc_match_file(pattern)
            if candidate and os.path.exists(candidate):
                regular = candidate
                break
    if bold is None:
        if regular is not None:
            candidate = _fc_match_file("sans-serif:lang=zh-cn:weight=bold")
            bold = candidate if candidate and os.path.exists(candidate) else regular
        else:
            bold = None

    if regular is None:
        scanned = _scan_cjk_fonts()
        if scanned:
            regular = str(scanned[0])
            bold_picks = [
                str(p) for p in scanned if any(k in p.name.lower() for k in _BOLD_NAME_KEYWORDS)
            ]
            bold = bold_picks[0] if bold_picks else regular
    return regular, bold


_REGULAR_FILE, _BOLD_FILE = _resolve_fonts()

if _REGULAR_FILE is None:
    logger.warning(
        "未找到任何中文字体，渲染图片中的中文将显示为方框；"
        "请安装中文字体（Debian/Ubuntu: fonts-noto-cjk，Arch: noto-fonts-cjk）"
    )


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = _BOLD_FILE if bold else _REGULAR_FILE
    if path:
        for index in (2, 0):
            try:
                return ImageFont.truetype(path, size, index=index)
            except OSError:
                continue
    return ImageFont.load_default(size)


def _truncate(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return f"{text}…" if text else ""


def _day_block_height(song_count: int) -> int:
    return DAY_HEADER_H + song_count * SONG_ROW_H


def render_history_image(range_label: str, days: list[dict], total: int) -> bytes:
    """把播放历史按天分块画成长图。

    days 为倒序的日列表，每项含 date 与 songs；
    songs 按时间正序，每项含 name/artist/album/source/requesters。
    """
    blocks = sum(_day_block_height(len(day["songs"])) for day in days)
    height = HEADER_H + 24 + blocks + DAY_GAP * max(0, len(days) - 1) + FOOTER_H
    canvas = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(canvas)

    f_title = _font(34, bold=True)
    f_sub = _font(22)
    f_day = _font(26, bold=True)
    f_count = _font(20)
    f_name = _font(26, bold=True)
    f_meta = _font(20)
    f_right = _font(20)
    f_right_bold = _font(20, bold=True)
    f_footer = _font(20)

    d.text((PAD, 20), "校园广播站 · 播放记录", font=f_title, fill=TEXT_MAIN)
    sub = f"{range_label}    共 {total} 首 · {len(days)} 天"
    d.text((PAD, 78), sub, font=f_sub, fill=TEXT_SUB)
    d.line((PAD, HEADER_H - 1, WIDTH - PAD, HEADER_H - 1), fill=DIVIDER, width=2)

    left_x = PAD + 18
    right_x = WIDTH - PAD - 18
    max_text_w = right_x - left_x - 170

    y = HEADER_H + 24
    for day in days:
        date = day["date"]
        songs = day["songs"]
        block_h = _day_block_height(len(songs))
        d.rounded_rectangle(
            (PAD, y, WIDTH - PAD, y + block_h),
            radius=12,
            fill=ROW_BG,
            outline=DIVIDER,
            width=2,
        )

        date_text = f"{date.month}月{date.day}日  {_WEEK_CN[date.weekday()]}"
        d.text((left_x, y + 14), date_text, font=f_day, fill=TEXT_MAIN)
        d.text((right_x, y + 18), f"{len(songs)} 首", font=f_count, fill=TEXT_SUB, anchor="ra")
        d.line(
            (PAD + 12, y + DAY_HEADER_H - 1, WIDTH - PAD - 12, y + DAY_HEADER_H - 1),
            fill=DIVIDER,
            width=1,
        )

        for i, song in enumerate(songs):
            row_y = y + DAY_HEADER_H + i * SONG_ROW_H
            name = song.get("name") or "未知歌曲"
            artist = song.get("artist") or "未知歌手"
            album = song.get("album") or ""
            meta = f"{artist} · {album}" if album else artist

            d.text(
                (left_x, row_y + 10),
                _truncate(d, name, f_name, max_text_w),
                font=f_name,
                fill=TEXT_MAIN,
            )
            d.text(
                (left_x, row_y + 40),
                _truncate(d, meta, f_meta, max_text_w),
                font=f_meta,
                fill=TEXT_SUB,
            )

            source = song.get("source") or ""
            d.text(
                (right_x, row_y + 10),
                SOURCE_NAMES.get(source, source),
                font=f_right_bold,
                fill=SOURCE_COLORS.get(source, ACCENT),
                anchor="ra",
            )
            d.text(
                (right_x, row_y + 40),
                f"点歌人数 {int(song.get('requesters') or 0)}",
                font=f_right,
                fill=TEXT_SUB,
                anchor="ra",
            )

            if i < len(songs) - 1:
                d.line(
                    (PAD + 12, row_y + SONG_ROW_H - 1, WIDTH - PAD - 12, row_y + SONG_ROW_H - 1),
                    fill=DIVIDER,
                    width=1,
                )

        y += block_h + DAY_GAP

    footer = "生成于 " + datetime.now().strftime("%Y-%m-%d %H:%M")
    fy = y - DAY_GAP + (FOOTER_H - 24) / 2
    d.text((WIDTH / 2, fy), footer, font=f_footer, fill=TEXT_SUB, anchor="ma")

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()
