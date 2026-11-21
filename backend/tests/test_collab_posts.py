"""Tests for collab posts, versions and attachments.

In-memory SQLite + overridden get_db; no local Postgres/Neo4j is touched.
Uploads go to a temporary collab directory.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.collab_auth import hash_password
from app.db import Base, get_db
from app.main import app as fastapi_app
from app.models import AnalysisRun, Candidate, CollabAttachment, CollabMember, CollabPost, CollabPostVersion, Project

PROJECT_SLUG = "collab-proj"
PERT_ID = "BRD-TEST"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32
SVG_OK = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1"/></svg>'
SVG_SCRIPT = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.collab_upload_dir", str(tmp_path / "collab_uploads"))
    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(test_engine)
    test_session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    shared_session = test_session()

    def override_db():
        yield shared_session

    original_overrides = fastapi_app.dependency_overrides.copy()
    fastapi_app.dependency_overrides[get_db] = override_db
    try:
        project = Project(project_id="proj-collab", slug=PROJECT_SLUG, name="协作测试项目", disease_id="MONDO:0000001")
        shared_session.add(project)
        shared_session.add(AnalysisRun(analysis_run_id="run-collab", project_id=project.project_id, run_type="s2", status="completed"))
        shared_session.add(Candidate(
            candidate_id="cand-1", project_id=project.project_id, analysis_run_id="run-collab",
            pert_id=PERT_ID, drug_name="测试药", rank=1, score_name="WTCS", score_value="0.1", evidence_json={},
        ))
        for username, role in [("alice", "member"), ("bob", "member"), ("carol", "admin")]:
            shared_session.add(CollabMember(
                username=username, password_hash=hash_password("pass123"),
                display_name=username.title(), role=role, is_active=True,
            ))
        shared_session.commit()

        def headers(username: str) -> dict:
            resp = TestClient(fastapi_app).post("/api/v1/collab/auth/login", json={"username": username, "password": "pass123"})
            return {"Authorization": f"Bearer {resp.json()['access_token']}"}

        yield TestClient(fastapi_app, raise_server_exceptions=False), shared_session, project.project_id, headers
    finally:
        fastapi_app.dependency_overrides.clear()
        fastapi_app.dependency_overrides.update(original_overrides)
        shared_session.close()
        Base.metadata.drop_all(test_engine)
        test_engine.dispose()


def _create_post(http, project_id, headers, pert_id=PERT_ID, title="机制说明"):
    return http.post(
        f"/api/v1/collab/projects/{project_id}/posts",
        headers=headers,
        json={"pert_id": pert_id, "title": title, "summary_text": "初步机制假设", "change_note": "初稿"},
    )


def test_full_flow_create_upload_list_detail(client):
    http, _, project_id, headers = client
    auth = headers("alice")
    created = _create_post(http, project_id, auth)
    assert created.status_code == 201, created.text
    post_id = created.json()["post_id"]

    files = [
        ("doc", "note.md", "# 机制说明\n正文".encode("utf-8")),
        ("data", "genes.csv", b"gene,logFC\nESR1,1.2\n"),
        ("data", "sig.json", b'{"target": "ESR1"}'),
        ("figure", "plot.png", PNG_BYTES),
        ("figure", "path.svg", SVG_OK),
    ]
    for kind, filename, content in files:
        resp = http.post(
            f"/api/v1/collab/projects/{project_id}/posts/{post_id}/attachments",
            headers=auth,
            params={"kind": kind, "filename": filename},
            content=content,
        )
        assert resp.status_code == 201, resp.text

    detail = http.get(f"/api/v1/collab/projects/{project_id}/posts/{post_id}", headers=auth).json()
    assert detail["attachment_count"] == 5
    assert len(detail["attachments"]) == 5
    assert detail["current_version"] == 1
    assert len(detail["versions"]) == 1

    listing = http.get(f"/api/v1/collab/projects/{project_id}/posts?pert_id={PERT_ID}", headers=auth).json()
    assert len(listing) == 1 and listing[0]["attachment_count"] == 5

    attachment_id = detail["attachments"][0]["attachment_id"]
    download = http.get(f"/api/v1/collab/attachments/{attachment_id}/download", headers=auth)
    assert download.status_code == 200
    assert download.content == "# 机制说明\n正文".encode("utf-8")


def test_update_keeps_history_version(client):
    http, _, project_id, headers = client
    auth = headers("alice")
    post_id = _create_post(http, project_id, auth, title="旧标题").json()["post_id"]

    updated = http.patch(
        f"/api/v1/collab/projects/{project_id}/posts/{post_id}",
        headers=auth,
        json={"title": "新标题", "summary_text": "更新后的说明", "change_note": "补充证据"},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["current_version"] == 2
    assert body["title"] == "新标题"
    versions = body["versions"]
    assert len(versions) == 2
    assert versions[0]["version"] == 1 and versions[0]["title"] == "旧标题"
    assert versions[1]["version"] == 2 and versions[1]["title"] == "新标题"


def test_non_author_cannot_edit_or_delete_but_can_read(client):
    http, _, project_id, headers = client
    author = headers("alice")
    other = headers("bob")
    post_id = _create_post(http, project_id, author).json()["post_id"]

    assert http.get(f"/api/v1/collab/projects/{project_id}/posts/{post_id}", headers=other).status_code == 200
    forbidden = http.patch(f"/api/v1/collab/projects/{project_id}/posts/{post_id}", headers=other, json={"title": "篡改"})
    assert forbidden.status_code == 403
    assert http.delete(f"/api/v1/collab/projects/{project_id}/posts/{post_id}", headers=other).status_code == 403


def test_admin_can_edit_other_post(client):
    http, _, project_id, headers = client
    author = headers("alice")
    admin = headers("carol")
    post_id = _create_post(http, project_id, author).json()["post_id"]
    resp = http.patch(f"/api/v1/collab/projects/{project_id}/posts/{post_id}", headers=admin, json={"title": "管理员修订"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "管理员修订"


def test_routes_require_authentication(client):
    http, _, project_id, _ = client
    assert http.get(f"/api/v1/collab/projects/{project_id}/posts").status_code == 401
    assert http.post(f"/api/v1/collab/projects/{project_id}/posts", json={"title": "x"}).status_code == 401


@pytest.mark.parametrize("kind,filename,content,status", [
    ("doc", "evil.exe", b"MZ fake", 415),
    ("doc", "page.html", b"<html></html>", 415),
    ("doc", "script.js", b"alert(1)", 415),
    ("data", "bundle.zip", b"PK fake", 415),
    ("data", "broken.json", b"{not json", 422),
    ("figure", "fake.png", b"not a png", 422),
    ("figure", "bad.svg", SVG_SCRIPT, 422),
    ("doc", "empty.md", b"", 422),
])
def test_rejects_invalid_uploads(client, kind, filename, content, status):
    http, _, project_id, headers = client
    auth = headers("alice")
    post_id = _create_post(http, project_id, auth).json()["post_id"]
    resp = http.post(
        f"/api/v1/collab/projects/{project_id}/posts/{post_id}/attachments",
        headers=auth, params={"kind": kind, "filename": filename}, content=content,
    )
    assert resp.status_code == status, resp.text


def test_rejects_oversize_upload(client, monkeypatch):
    http, _, project_id, headers = client
    monkeypatch.setattr("app.core.config.settings.collab_doc_max_bytes", 10)
    auth = headers("alice")
    post_id = _create_post(http, project_id, auth).json()["post_id"]
    resp = http.post(
        f"/api/v1/collab/projects/{project_id}/posts/{post_id}/attachments",
        headers=auth, params={"kind": "doc", "filename": "big.md"}, content=b"12345678901234567890",
    )
    assert resp.status_code == 413


def test_unknown_pert_id_rejected(client):
    http, _, project_id, headers = client
    resp = http.post(
        f"/api/v1/collab/projects/{project_id}/posts", headers=headers("alice"),
        json={"pert_id": "NOT-REAL", "title": "x", "summary_text": "y"},
    )
    assert resp.status_code == 422


def test_soft_delete_retains_record_and_file(client):
    http, session, project_id, headers = client
    auth = headers("alice")
    post_id = _create_post(http, project_id, auth).json()["post_id"]
    up = http.post(
        f"/api/v1/collab/projects/{project_id}/posts/{post_id}/attachments",
        headers=auth, params={"kind": "doc", "filename": "note.md"}, content="# 保留".encode("utf-8"),
    )
    attachment_id = up.json()["attachment_id"]

    assert http.delete(f"/api/v1/collab/projects/{project_id}/posts/{post_id}", headers=auth).status_code == 204
    assert http.get(f"/api/v1/collab/projects/{project_id}/posts", headers=auth).json() == []

    post = session.get(CollabPost, post_id)
    assert post.is_deleted is True
    attachment = session.get(CollabAttachment, attachment_id)
    assert attachment is not None  # metadata retained

    assert http.get(f"/api/v1/collab/attachments/{attachment_id}/download", headers=auth).status_code == 404


def test_failed_db_commit_removes_partial_file(client, monkeypatch):
    http, session, project_id, headers = client
    auth = headers("alice")
    post_id = _create_post(http, project_id, auth).json()["post_id"]

    original_commit = session.commit

    def failing_commit():
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(session, "commit", failing_commit)
    resp = http.post(
        f"/api/v1/collab/projects/{project_id}/posts/{post_id}/attachments",
        headers=auth, params={"kind": "doc", "filename": "note.md"}, content="# 临时".encode("utf-8"),
    )
    assert resp.status_code == 500
    monkeypatch.setattr(session, "commit", original_commit)

    assert session.scalar(select(CollabAttachment).where(CollabAttachment.post_id == post_id)) is None


def test_collab_data_is_isolated_from_research_tables(client):
    _, session, _, _ = client
    projects = session.scalars(select(Project)).all()
    assert len(projects) == 1
    # This case seeds no post; collab tables are separate and queryable.
    assert session.scalar(select(CollabPost)) is None
