"""Tests for the isolated collaboration-center authentication.

Uses an in-memory SQLite database and overrides get_db, so no local
Postgres/Neo4j service is touched. Existing research tables are not modified.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.collab_auth import hash_password
from app.db import Base, get_db
from app.main import app as fastapi_app
from app.models import CollabMember, Project


@pytest.fixture()
def client():
    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(test_engine)
    test_session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)

    def override_db():
        with test_session() as session:
            yield session

    original_overrides = fastapi_app.dependency_overrides.copy()
    fastapi_app.dependency_overrides[get_db] = override_db
    try:
        with test_session() as session:
            session.add(CollabMember(
                username="admin",
                password_hash=hash_password("secret123"),
                display_name="管理员",
                role="admin",
                is_active=True,
            ))
            session.add(CollabMember(
                username="disabled",
                password_hash=hash_password("disabled123"),
                display_name="已停用成员",
                role="member",
                is_active=False,
            ))
            session.commit()
        yield TestClient(fastapi_app), test_session
    finally:
        fastapi_app.dependency_overrides.clear()
        fastapi_app.dependency_overrides.update(original_overrides)
        Base.metadata.drop_all(test_engine)
        test_engine.dispose()


def _login(client: TestClient, username: str, password: str):
    return client.post("/api/v1/collab/auth/login", json={"username": username, "password": password})


def test_login_success_returns_token_and_member(client):
    http, _ = client
    resp = _login(http, "admin", "secret123")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["member"]["username"] == "admin"
    assert body["member"]["role"] == "admin"

    me = http.get("/api/v1/collab/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200, me.text
    assert me.json()["member_id"] == body["member"]["member_id"]


def test_login_wrong_password_is_rejected(client):
    http, _ = client
    assert _login(http, "admin", "wrong-pass").status_code == 401


def test_login_unknown_user_is_rejected(client):
    http, _ = client
    assert _login(http, "nobody", "whatever").status_code == 401


def test_disabled_member_cannot_login(client):
    http, _ = client
    assert _login(http, "disabled", "disabled123").status_code == 401


def test_me_requires_valid_token(client):
    http, _ = client
    assert http.get("/api/v1/collab/auth/me").status_code == 401
    assert http.get("/api/v1/collab/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_collab_table_is_isolated_from_research_projects(client):
    _, test_session = client
    with test_session() as session:
        assert session.scalar(select(Project)) is None
        member = session.scalar(select(CollabMember).where(CollabMember.username == "admin"))
        assert member is not None and member.role == "admin"
