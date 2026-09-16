"""后端配置（独立项目，只读环境变量；app.py 启动时已把 backend/.env 注入环境变量）。"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


class Settings:
    def __init__(self) -> None:
        self.database_url: str = _env("DATABASE_URL")
        self.music_api_base: str = _env("QQ_MUSIC_API_BASE", "http://127.0.0.1:8081").rstrip("/")
        self.cover_dir: Path = Path(
            _env("QQ_MUSIC_COVER_DIR") or str(PROJECT_ROOT / "data" / "covers")
        )
        self.permissions_file: Path = Path(
            _env("QQ_PERMISSIONS_FILE") or str(PROJECT_ROOT / "data" / "permissions.json")
        )

        self.llm_api_base: str = _env("LLM_API_BASE").rstrip("/")
        self.llm_api_key: str = _env("LLM_API_KEY")
        self.llm_model: str = _env("LLM_MODEL", "deepseek-chat")
        self.llm_timeout: float = float(_env("LLM_TIMEOUT", "30") or 30)

        self.web_admin_username: str = _env("WEB_ADMIN_USERNAME", "admin")
        self.web_admin_password: str = _env("WEB_ADMIN_PASSWORD")
        self.web_admin_token: str = _env("WEB_ADMIN_TOKEN")
        self.web_admin_port: int = _env_int("WEB_ADMIN_PORT", 8600)
        self.web_cors_origins: list[str] = [
            o.strip() for o in _env("WEB_ADMIN_CORS", "*").split(",") if o.strip()
        ]


settings = Settings()
