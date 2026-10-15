from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from jose import ExpiredSignatureError, JWTError, jwt

from app.config import OAuthConfigError, OAuthSettings, get_oauth_settings, get_settings
from app.errors import ApiError


logger = logging.getLogger("grid_platform")
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


def oauth_settings() -> OAuthSettings:
    try:
        return get_oauth_settings()
    except OAuthConfigError as exc:
        logger.error("Gitea OAuth is not configured: %s", exc)
        raise ApiError(503, "GITEA_OAUTH_NOT_CONFIGURED") from exc


def authorization_url(state: str) -> str:
    settings = oauth_settings()
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
    except HTTPError as exc:
        # Gitea explains token failures (invalid_client, invalid_grant, ...)
        # in the body; it never contains the client secret.
        try:
            detail = exc.read(500).decode("utf-8", "replace")
        except OSError:
            detail = ""
        logger.error(
            "Gitea OAuth request failed: %s %s -> HTTP %s %s",
            request.get_method(), request.full_url, exc.code, detail,
        )
        raise ApiError(502, "GITEA_OAUTH_REJECTED") from exc
    except (URLError, TimeoutError, OSError) as exc:
        logger.error(
            "Gitea OAuth request failed: %s %s -> %s",
            request.get_method(), request.full_url, getattr(exc, "reason", exc),
        )
        raise ApiError(502, "GITEA_OAUTH_UNAVAILABLE") from exc
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.error("Gitea OAuth returned non-JSON from %s", request.full_url)
        raise ApiError(502, "GITEA_OAUTH_INVALID_RESPONSE") from exc
    if not isinstance(payload, dict):
        raise ApiError(502, "GITEA_OAUTH_INVALID_RESPONSE")
    return payload


def exchange_code(code: str) -> str:
    settings = oauth_settings()
    # RFC 6749 section 4.1.3: the token request is form encoded.
    body = urlencode(
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
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
    )
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ApiError(502, "GITEA_OAUTH_INVALID_RESPONSE")
    return access_token


def fetch_userinfo(access_token: str) -> dict[str, Any]:
    settings = oauth_settings()
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
