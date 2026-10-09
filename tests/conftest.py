from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "test-secret-that-is-at-least-thirty-two-bytes")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:5500")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("GITEA_INTERNAL_URL", "http://gitea:3000")
os.environ.setdefault("GITEA_PUBLIC_URL", "http://gitea.test")
os.environ.setdefault("GITEA_OAUTH_CLIENT_ID", "test-client")
os.environ.setdefault("GITEA_OAUTH_CLIENT_SECRET", "test-secret")
os.environ.setdefault(
    "GITEA_OAUTH_REDIRECT_URI", "http://platform.test/auth/gitea/callback"
)
os.environ.setdefault("OAUTH_COOKIE_SECURE", "false")

import base64
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app import gitea_oauth
from app.main import app


engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


@event.listens_for(engine, "connect")
def enable_foreign_keys(dbapi_connection, _record) -> None:
    dbapi_connection.execute("PRAGMA foreign_keys = ON")


TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base.metadata.create_all(engine)


def override_db():
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


app.dependency_overrides[get_db] = override_db


@pytest.fixture(autouse=True)
def clean_database():
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
    yield


@pytest.fixture(autouse=True)
def fake_gitea(monkeypatch):
    monkeypatch.setattr(gitea_oauth, "exchange_code", lambda code: code)

    def fake_userinfo(encoded_profile: str):
        padding = "=" * (-len(encoded_profile) % 4)
        return json.loads(base64.urlsafe_b64decode(encoded_profile + padding))

    monkeypatch.setattr(gitea_oauth, "fetch_userinfo", fake_userinfo)


@pytest.fixture
def client():
    with TestClient(
        app,
        base_url="http://platform.test",
        raise_server_exceptions=False,
    ) as test_client:
        yield test_client


@pytest.fixture
def db_session():
    with TestingSession() as session:
        yield session
