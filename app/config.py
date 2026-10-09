from __future__ import annotations

import os
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def default_database_url() -> str:
    return f"sqlite:///{(PROJECT_ROOT / 'grid_platform.db').as_posix()}"


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    cors_origins: tuple[str, ...]
    gitea_internal_url: str
    gitea_public_url: str
    gitea_oauth_client_id: str
    gitea_oauth_client_secret: str
    gitea_oauth_redirect_uri: str
    oauth_cookie_secure: bool


@lru_cache
def get_settings() -> Settings:
    """Load and validate process-level application settings."""
    secret = os.getenv("JWT_SECRET")
    if secret is None:
        raise RuntimeError("JWT_SECRET 環境變數為必填")
    if len(secret.encode("utf-8")) < 32:
        raise RuntimeError("JWT_SECRET 必須至少為 32 bytes")

    origins = tuple(
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "").split(",")
        if origin.strip()
    )
    if "*" in origins:
        raise RuntimeError("CORS_ORIGINS 不可使用萬用來源 *")

    oauth_file = Path(
        os.getenv("GITEA_OAUTH_CONFIG_FILE", "/run/grid-oauth/client.json")
    )
    oauth_config: dict[str, str] = {}
    if oauth_file.is_file():
        try:
            raw_config = json.loads(oauth_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"無法讀取 Gitea OAuth 設定檔: {oauth_file}") from exc
        if not isinstance(raw_config, dict):
            raise RuntimeError(f"Gitea OAuth 設定檔格式錯誤: {oauth_file}")
        oauth_config = {
            str(name): str(value)
            for name, value in raw_config.items()
            if value is not None
        }

    def oauth_value(environment_name: str, file_name: str) -> str | None:
        return os.getenv(environment_name) or oauth_config.get(file_name)

    oauth_values = {
        "GITEA_INTERNAL_URL": oauth_value(
            "GITEA_INTERNAL_URL", "gitea_internal_url"
        ),
        "GITEA_PUBLIC_URL": oauth_value("GITEA_PUBLIC_URL", "gitea_public_url"),
        "GITEA_OAUTH_CLIENT_ID": oauth_value(
            "GITEA_OAUTH_CLIENT_ID", "gitea_oauth_client_id"
        ),
        "GITEA_OAUTH_CLIENT_SECRET": oauth_value(
            "GITEA_OAUTH_CLIENT_SECRET", "gitea_oauth_client_secret"
        ),
        "GITEA_OAUTH_REDIRECT_URI": oauth_value(
            "GITEA_OAUTH_REDIRECT_URI", "gitea_oauth_redirect_uri"
        ),
    }
    missing = [name for name, value in oauth_values.items() if not value]
    if missing:
        raise RuntimeError(f"缺少 Gitea OAuth 設定: {', '.join(missing)}")

    redirect_uri = oauth_values["GITEA_OAUTH_REDIRECT_URI"] or ""
    secure_default = redirect_uri.lower().startswith("https://")
    secure_value = os.getenv("OAUTH_COOKIE_SECURE")
    oauth_cookie_secure = (
        secure_default
        if secure_value is None
        else secure_value.strip().lower() in {"1", "true", "yes", "on"}
    )

    return Settings(
        database_url=os.getenv("DATABASE_URL", default_database_url()),
        jwt_secret=secret,
        cors_origins=origins,
        gitea_internal_url=(oauth_values["GITEA_INTERNAL_URL"] or "").rstrip("/"),
        gitea_public_url=(oauth_values["GITEA_PUBLIC_URL"] or "").rstrip("/"),
        gitea_oauth_client_id=oauth_values["GITEA_OAUTH_CLIENT_ID"] or "",
        gitea_oauth_client_secret=oauth_values["GITEA_OAUTH_CLIENT_SECRET"] or "",
        gitea_oauth_redirect_uri=redirect_uri,
        oauth_cookie_secure=oauth_cookie_secure,
    )
