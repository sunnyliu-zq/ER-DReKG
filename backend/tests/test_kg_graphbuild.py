"""Stage 2A-4.1 GraphBuild consistency tests for KG06/KG07."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main as main_module
from app.core.config import settings
from app.db import Base
from app.interpretation.evidence import EvidenceError, resolve_graph_build
from app.main import create_project
from app.models import AnalysisRun, GraphBuild
from app.schemas import ProjectCreate


@pytest.fixture
def db():
    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(test_engine)
    test_session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    session = test_session()
    yield session
    session.close()
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()


def _new_project(db):
    return create_project(
        ProjectCreate(
            slug=f"kg-graphbuild-{uuid4().hex[:8]}",
            name="KG GraphBuild 测试",
            disease_id="MONDO:0000001",
        ),
        db,
    )


def test_resolve_graph_build_requires_project_materialized_neo4j(db, monkeypatch):
    monkeypatch.setattr(settings, "graph_source", "neo4j")
    project = _new_project(db)
    other = _new_project(db)
    run = AnalysisRun(
        analysis_run_id=f"ANALYSIS_RUN:KG_GRAPH_BUNDLE:{uuid4()}",
        project_id=project.project_id,
        run_type="kg_graph_bundle",
        status="completed",
        parameters={},
        software_versions={},
        input_hashes={},
    )
    db.add(run)
    db.flush()

    valid_id = f"GRAPH_BUILD:{project.project_id}:valid"
    importing_id = f"GRAPH_BUILD:{project.project_id}:importing"
    failed_id = f"GRAPH_BUILD:{project.project_id}:failed"
    csv_snapshot_id = f"GRAPH_BUILD:{project.project_id}:csv-snapshot"
    csv_materialized_id = f"GRAPH_BUILD:{project.project_id}:csv-materialized"
    other_project_id = f"GRAPH_BUILD:{other.project_id}:valid"
    db.add_all([
        GraphBuild(graph_build_id=valid_id, project_id=project.project_id, analysis_run_id=run.analysis_run_id, build_key="a" * 64, status="materialized", graph_source="neo4j", input_hashes={}, summary_json={}, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        GraphBuild(graph_build_id=importing_id, project_id=project.project_id, analysis_run_id=run.analysis_run_id, build_key="b" * 64, status="importing", graph_source="neo4j", input_hashes={}, summary_json={}, created_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
        GraphBuild(graph_build_id=failed_id, project_id=project.project_id, analysis_run_id=run.analysis_run_id, build_key="c" * 64, status="failed", graph_source="neo4j", input_hashes={}, summary_json={}, created_at=datetime(2026, 1, 3, tzinfo=timezone.utc)),
        GraphBuild(graph_build_id=csv_snapshot_id, project_id=project.project_id, analysis_run_id=run.analysis_run_id, build_key="d" * 64, status="materialized_snapshot", graph_source="csv", input_hashes={}, summary_json={}, created_at=datetime(2026, 1, 4, tzinfo=timezone.utc)),
        GraphBuild(graph_build_id=csv_materialized_id, project_id=project.project_id, analysis_run_id=run.analysis_run_id, build_key="e" * 64, status="materialized", graph_source="csv", input_hashes={}, summary_json={}, created_at=datetime(2026, 1, 5, tzinfo=timezone.utc)),
        GraphBuild(graph_build_id=other_project_id, project_id=other.project_id, analysis_run_id=run.analysis_run_id, build_key="f" * 64, status="materialized", graph_source="neo4j", input_hashes={}, summary_json={}, created_at=datetime(2026, 1, 6, tzinfo=timezone.utc)),
    ])
    db.commit()

    assert resolve_graph_build(db, project.project_id, valid_id).graph_build_id == valid_id
    assert resolve_graph_build(db, project.project_id, None).graph_build_id == valid_id
    with pytest.raises(LookupError):
        resolve_graph_build(db, project.project_id, other_project_id)
    for rejected_id in (importing_id, failed_id, csv_snapshot_id, csv_materialized_id):
        with pytest.raises(EvidenceError):
            resolve_graph_build(db, project.project_id, rejected_id)

    with pytest.raises(HTTPException) as other_project_error:
        main_module._resolve_kg_graph_build_or_http(db, project.project_id, other_project_id)
    assert other_project_error.value.status_code == 404
    for rejected_id in (importing_id, failed_id, csv_snapshot_id, csv_materialized_id):
        with pytest.raises(HTTPException) as rejected_error:
            main_module._resolve_kg_graph_build_or_http(db, project.project_id, rejected_id)
        assert rejected_error.value.status_code == 409

    empty_project = _new_project(db)
    with pytest.raises(HTTPException) as no_build_error:
        main_module._resolve_kg_graph_build_or_http(db, empty_project.project_id, None)
    assert no_build_error.value.status_code == 404


def test_kg07_route_reuses_one_build_and_passes_expected_to_kg07(db, monkeypatch):
    project = _new_project(db)
    build_id = "GRAPH_BUILD:route-test:neo4j"
    build = SimpleNamespace(
        graph_build_id=build_id,
        project_id=project.project_id,
        status="materialized",
        graph_source="neo4j",
    )
    package = {"graph_build_id": build_id, "candidate": {"pert_id": "BRD-K44227013"}}
    calls = {}

    monkeypatch.setattr(main_module, "resolve_kg_graph_build", lambda db_arg, project_id, requested: build)

    def fake_package(db_arg, project_arg, build_arg, pert_id):
        calls["package"] = (project_arg.project_id, build_arg.graph_build_id, pert_id)
        return package

    def fake_interpret(package_arg, mode, *, expected_graph_build_id=None):
        calls["interpret"] = (package_arg["graph_build_id"], mode, expected_graph_build_id)
        return {"graph_build_id": expected_graph_build_id, "mode": mode}

    def fake_rule(package_arg, *, expected_graph_build_id=None):
        calls["rule"] = (package_arg["graph_build_id"], expected_graph_build_id)
        return {"graph_build_id": expected_graph_build_id, "mode": "kg_rule"}

    monkeypatch.setattr(main_module, "_kg06_evidence_package_for_build", fake_package)
    monkeypatch.setattr(main_module, "kg07_interpret", fake_interpret)
    monkeypatch.setattr(main_module, "kg07_rule_template", fake_rule)

    result = main_module.kg07_interpretation(
        project.project_id,
        "BRD-K44227013",
        mode="kg_llm",
        graph_build_id=build_id,
        db=db,
    )
    assert calls["package"] == (project.project_id, build_id, "BRD-K44227013")
    assert calls["interpret"] == (build_id, "kg_llm", build_id)
    assert result["graph_build_id"] == build_id

    result = main_module.kg07_interpretation(
        project.project_id,
        "BRD-K44227013",
        mode="kg_rule",
        graph_build_id=build_id,
        db=db,
    )
    assert calls["rule"] == (build_id, build_id)
    assert result["graph_build_id"] == build_id
