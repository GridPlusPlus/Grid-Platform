from __future__ import annotations

import io
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

from jose import jwt
from sqlalchemy import func, select

from app import gitea_oauth
from app.gitea_oauth import exchange_code as real_exchange_code
from app.config import get_settings
from app.errors import ApiError
from app.main import _upsert_gitea_user
from app.models import User
from tests.helpers import auth, login


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_gitea_login_redirect_uses_oidc_and_state_cookie(client):
    response = client.get("/auth/gitea/login", follow_redirects=False)
    assert response.status_code == 307
    target = urlparse(response.headers["location"])
    assert f"{target.scheme}://{target.netloc}" == "http://gitea.test"
    assert target.path == "/login/oauth/authorize"
    query = parse_qs(target.query)
    assert query["client_id"] == ["test-client"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["read:user"]
    assert query["redirect_uri"] == ["http://platform.test/auth/gitea/callback"]
    assert query["state"][0]
    cookie = response.headers["set-cookie"]
    assert "grid_gitea_oauth_state=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie


def test_gitea_login_redirects_to_callback_host_before_setting_state(client):
    response = client.get(
        "/auth/gitea/login",
        headers={"host": "127.0.0.1:8000"},
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert (
        response.headers["location"]
        == "http://platform.test/auth/gitea/login?canonical=1"
    )
    assert "grid_gitea_oauth_state=" not in response.headers.get("set-cookie", "")


def test_gitea_login_redirects_when_scheme_differs_from_callback(client):
    response = client.get(
        "/auth/gitea/login",
        headers={"x-forwarded-host": "platform.test"},
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert urlparse(response.headers["location"]).netloc == "gitea.test"

    response = client.get(
        "https://platform.test/auth/gitea/login", follow_redirects=False
    )
    assert response.status_code == 307
    assert (
        response.headers["location"]
        == "http://platform.test/auth/gitea/login?canonical=1"
    )


def test_gitea_login_does_not_loop_when_proxy_rewrites_host(client):
    response = client.get(
        "/auth/gitea/login",
        params={"canonical": "1"},
        headers={"host": "gridplusplus"},
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert urlparse(response.headers["location"]).netloc == "gitea.test"
    assert "grid_gitea_oauth_state=" in response.headers["set-cookie"]


def test_gitea_login_reports_missing_oauth_configuration(client, monkeypatch, tmp_path):
    for name in (
        "GITEA_INTERNAL_URL",
        "GITEA_PUBLIC_URL",
        "GITEA_OAUTH_CLIENT_ID",
        "GITEA_OAUTH_CLIENT_SECRET",
        "GITEA_OAUTH_REDIRECT_URI",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITEA_OAUTH_CONFIG_FILE", str(tmp_path / "missing.json"))
    response = client.get("/auth/gitea/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/#auth_error=GITEA_OAUTH_NOT_CONFIGURED"


def test_callback_creates_one_user_and_issues_platform_jwt(client, db_session):
    first = login(client, "User@Example.COM", username="GiteaUser")
    second = login(client, "user@example.com", username="ChangedAtProvider")

    users = db_session.scalars(select(User)).all()
    assert len(users) == 1
    assert users[0].gitea_user_id
    assert users[0].username == "GiteaUser"
    assert users[0].email == "user@example.com"

    for token in (first, second):
        payload = jwt.decode(
            token,
            get_settings().jwt_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        assert int(payload["sub"]) == users[0].id
        assert payload["exp"] - payload["iat"] == 3600


def test_gitea_api_user_profile_is_accepted(db_session):
    user = _upsert_gitea_user(
        db_session,
        {"id": 123, "login": "gitea-user", "email": "user@example.com"},
    )
    assert user.gitea_user_id == "123"
    assert user.username == "gitea-user"
    assert user.email == "user@example.com"


def test_first_oauth_login_links_existing_email_and_preserves_data(client, db_session):
    existing = User(
        username="legacy-name",
        email="legacy@example.com",
        password_hash="old-unused-hash",
    )
    db_session.add(existing)
    db_session.commit()
    existing_id = existing.id

    token = login(client, "legacy@example.com", username="gitea-name")
    me = client.get("/users/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["id"] == existing_id
    assert me.json()["username"] == "legacy-name"
    db_session.expire_all()
    assert db_session.get(User, existing_id).gitea_user_id is not None
    assert db_session.scalar(select(func.count()).select_from(User)) == 1


def test_local_password_auth_endpoints_are_removed(client):
    assert client.post("/auth/register", json={}).status_code == 404
    assert client.post("/auth/login", json={}).status_code == 404


def test_callback_rejects_missing_or_mismatched_state(client):
    missing_cookie = client.get(
        "/auth/gitea/callback",
        params={"code": "code", "state": "not-signed"},
        follow_redirects=False,
    )
    assert missing_cookie.status_code == 303
    assert (
        missing_cookie.headers["location"]
        == "/#auth_error=GITEA_OAUTH_STATE_INVALID"
    )

    start = client.get("/auth/gitea/login", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    mismatched = client.get(
        "/auth/gitea/callback",
        params={"code": "code", "state": state + "x"},
        follow_redirects=False,
    )
    assert mismatched.status_code == 303
    assert mismatched.headers["location"] == "/#auth_error=GITEA_OAUTH_STATE_INVALID"


def test_callback_reports_denied_authorization(client):
    response = client.get(
        "/auth/gitea/callback",
        params={"error": "access_denied", "state": "anything"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/#auth_error=GITEA_OAUTH_DENIED"


def test_callback_reports_gitea_token_failure(client, monkeypatch):
    def reject(code):
        raise ApiError(502, "GITEA_OAUTH_REJECTED")

    monkeypatch.setattr(gitea_oauth, "exchange_code", reject)
    start = client.get("/auth/gitea/login", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    response = client.get(
        "/auth/gitea/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/#auth_error=GITEA_OAUTH_REJECTED"


def test_logout_revokes_only_that_access_token(client):
    first = login(client)
    second = login(client)

    revoked = client.post("/auth/logout", headers=auth(first))
    assert revoked.status_code == 204
    assert client.get("/users/me", headers=auth(first)).status_code == 401
    assert client.get("/users/me", headers=auth(second)).status_code == 200


def test_access_token_without_jti_is_rejected(client):
    token = login(client)
    payload = jwt.decode(
        token,
        get_settings().jwt_secret,
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
    del payload["jti"]
    forged = jwt.encode(payload, get_settings().jwt_secret, algorithm="HS256")

    response = client.get("/users/me", headers=auth(forged))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_TOKEN_INVALID"



class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_token_exchange_is_form_encoded_and_maps_gitea_errors(monkeypatch):
    sent = {}

    def fake_urlopen(request, timeout):
        sent["body"] = request.data.decode()
        sent["content_type"] = request.get_header("Content-type")
        return _FakeResponse(b'{"access_token": "gitea-token"}')

    monkeypatch.setattr(gitea_oauth, "urlopen", fake_urlopen)
    assert real_exchange_code("the-code") == "gitea-token"
    assert sent["content_type"] == "application/x-www-form-urlencoded"
    body = parse_qs(sent["body"])
    assert body["grant_type"] == ["authorization_code"]
    assert body["client_secret"] == ["test-secret"]
    assert body["code"] == ["the-code"]

    def rejecting_urlopen(request, timeout):
        raise HTTPError(
            request.full_url, 400, "Bad Request", {},
            io.BytesIO(b'{"error":"invalid_client"}'),
        )

    monkeypatch.setattr(gitea_oauth, "urlopen", rejecting_urlopen)
    with pytest.raises(ApiError) as rejected:
        real_exchange_code("the-code")
    assert rejected.value.code == "GITEA_OAUTH_REJECTED"

    def unreachable_urlopen(request, timeout):
        raise URLError("Name or service not known")

    monkeypatch.setattr(gitea_oauth, "urlopen", unreachable_urlopen)
    with pytest.raises(ApiError) as unreachable:
        real_exchange_code("the-code")
    assert unreachable.value.code == "GITEA_OAUTH_UNAVAILABLE"
