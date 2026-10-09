from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from jose import ExpiredSignatureError, JWTError, jwt

from app.config import get_settings
from app.errors import ApiError


STATE_ALGORITHM = "HS256"
STATE_TTL_SECONDS = 600


def create_oauth_state() -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "purpose": "gitea_oauth_state",
            "csrf": secrets.token_urlsafe(24),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=STATE_TTL_SECONDS)).timestamp()),
        },
        get_settings().jwt_secret,
        algorithm=STATE_ALGORITHM,
    )


def validate_oauth_state(query_state: str, cookie_state: str | None) -> None:
    if not cookie_state or not secrets.compare_digest(query_state, cookie_state):
        raise ApiError(401, "GITEA_OAUTH_STATE_INVALID")
    try:
        payload = jwt.decode(
            query_state,
            get_settings().jwt_secret,
            algorithms=[STATE_ALGORITHM],
            options={"verify_aud": False},
        )
    except (ExpiredSignatureError, JWTError) as exc:
        raise ApiError(401, "GITEA_OAUTH_STATE_INVALID") from exc
    if payload.get("purpose") != "gitea_oauth_state" or not payload.get("csrf"):
        raise ApiError(401, "GITEA_OAUTH_STATE_INVALID")


def authorization_url(state: str) -> str:
    settings = get_settings()
    query = urlencode(
        {
            "client_id": settings.gitea_oauth_client_id,
            "redirect_uri": settings.gitea_oauth_redirect_uri,
            "response_type": "code",
            "scope": "read:user",
            "state": state,
        }
    )
    return f"{settings.gitea_public_url}/login/oauth/authorize?{query}"


def _json_request(request: Request) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=10) as response:
            content = response.read()
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ApiError(502, "GITEA_OAUTH_UNAVAILABLE") from exc
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(502, "GITEA_OAUTH_INVALID_RESPONSE") from exc
    if not isinstance(payload, dict):
        raise ApiError(502, "GITEA_OAUTH_INVALID_RESPONSE")
    return payload


def exchange_code(code: str) -> str:
    settings = get_settings()
    body = json.dumps(
        {
            "client_id": settings.gitea_oauth_client_id,
            "client_secret": settings.gitea_oauth_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.gitea_oauth_redirect_uri,
        }
    ).encode("utf-8")
    payload = _json_request(
        Request(
            f"{settings.gitea_internal_url}/login/oauth/access_token",
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
    )
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ApiError(502, "GITEA_OAUTH_INVALID_RESPONSE")
    return access_token


def fetch_userinfo(access_token: str) -> dict[str, Any]:
    settings = get_settings()
    return _json_request(
        Request(
            f"{settings.gitea_internal_url}/api/v1/user",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            method="GET",
        )
    )
