import pytest
from uuid import uuid4
from pathlib import Path
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.bootstrap import seed_historical_project
from app.db import Base, get_db
from app.main import app as fastapi_app, _persist_graph_evidence_records, _persist_disease_contexts, _resolve_graph_build, _graph_build_queryable, _mark_graph_build_failed, candidates, compare_project_runs, create_project, create_study, drug_mechanisms, evidence, graph_build, graph_search, graph_summary, import_project_s2, job_status, link_dataset_to_study, list_datasets, list_graph_builds, list_projects, list_runs, list_studies, project_s2_conditions, project_s2_stability, register_dataset, run_precomputed_s1, run_project_s2, run_project_pathway_enrichment, s1_external_validation, s1_genes, s1_pathways, s1_summary, s1_volcano, s2_candidate_evidence, s2_evidence_matrix, subgraph, _graph_build_inputs, _graph_bundle_dir_for_build
from app.models import AnalysisRun, Dataset, DiseaseContext, EvidenceRecord, GraphBuild, Project, S2ConditionResult
from app.schemas import DatasetCreate, DatasetStudyLink, PrecomputedS1Request, ProjectCreate, S1EnrichmentRequest, S2ImportRequest, S2RunRequest, StudyCreate


@pytest.fixture
def db():
    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(test_engine)
    test_session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    session = test_session()
    seed_historical_project(session)
    yield session
    session.close()
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()


def get_erpos_project(db):
    return db.scalar(select(Project).where(Project.slug == "erpos-gse42568"))


def test_projects_s1_and_real_candidate_pagination(db):
    projects = list_projects(db)
    project = get_erpos_project(db)
    assert project in projects
    assert s1_summary(project.project_id, db)["significant_genes_fdr05"] == 7066
    page = candidates(project.project_id, offset=0, limit=10, db=db)
    assert len(page["items"]) == 10
    assert page["total"] == 50 and page["truncated"] is True


def test_phase_f_candidate_evidence_is_hash_verified_and_project_scoped(db):
    er_project = get_erpos_project(db)
    page = s2_candidate_evidence(er_project.project_id, offset=0, limit=10, db=db)
    next_page = s2_candidate_evidence(er_project.project_id, offset=10, limit=10, db=db)
    assert page["available"] is True
    assert page["total"] == 819
    assert len(page["items"]) == len(next_page["items"]) == 10
    assert page["source_sha256"] and page["graph_build_id"].endswith("a6aabb2147cb5f5e")
    assert {row["pert_id"] for row in page["items"]}.isdisjoint({row["pert_id"] for row in next_page["items"]})

    other = create_project(ProjectCreate(slug=f"phase-f-scope-{uuid4().hex[:8]}", name="Phase F 隔离测试", disease_id="MONDO:0000001"), db)
    unavailable = s2_candidate_evidence(other.project_id, offset=0, limit=10, db=db)
    assert unavailable["available"] is False and unavailable["items"] == []


def test_graph_evidence_is_persisted_and_idempotent_per_build(tmp_path, db):
    import csv
    import hashlib

    project = get_erpos_project(db)
    run_id = f"ANALYSIS_RUN:KG_GRAPH_BUNDLE:{uuid4()}"
    db.add(AnalysisRun(analysis_run_id=run_id, project_id=project.project_id, run_type="kg_graph_bundle", status="completed", parameters={}, software_versions={}, input_hashes={}))
    build_id = f"GRAPH_BUILD:{project.project_id}:{uuid4().hex[:16]}"
    graph_build_record = GraphBuild(graph_build_id=build_id, project_id=project.project_id, analysis_run_id=run_id, build_key=uuid4().hex, status="materialized", graph_source="neo4j", input_hashes={}, summary_json={})
    db.add(graph_build_record)
    db.commit()

    evidence_path = tmp_path / "evidence_records.csv"
    fields = ["evidence_id", "source_release", "source_record_id", "subject_id", "predicate", "object_id", "source_assertion_json"]
    evidence_id = hashlib.sha256(b"test-evidence").hexdigest()
    assertion = '{"source_urls":["https://example.org/source"],"support":"primary"}'
    with evidence_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"evidence_id": evidence_id, "source_release": "test release", "source_record_id": "record-1", "subject_id": "CHEMBL:TEST", "predicate": "TARGETS", "object_id": "ENSGTEST", "source_assertion_json": assertion})
    graph_build_record.input_hashes = {"evidence_records.csv": hashlib.sha256(evidence_path.read_bytes()).hexdigest()}
    db.commit()

    assert _persist_graph_evidence_records(db, graph_build_record, tmp_path) == 1
    assert _persist_graph_evidence_records(db, graph_build_record, tmp_path) == 1
    saved = db.scalar(select(EvidenceRecord).where(EvidenceRecord.graph_build_id == build_id, EvidenceRecord.evidence_id == evidence_id))
    assert saved is not None and saved.source_assertion["support"] == "primary"
    assert saved.source_url == ["https://example.org/source"]
    assert evidence(evidence_id, db, build_id)["assertion_sha256"] == saved.assertion_sha256
    assert db.scalar(select(func.count()).select_from(EvidenceRecord).where(EvidenceRecord.graph_build_id == build_id)) == 1

    evidence_path.write_text(evidence_path.read_text(encoding="utf-8").replace("primary", "changed"), encoding="utf-8")
    with pytest.raises(HTTPException) as changed_input:
        _persist_graph_evidence_records(db, graph_build_record, tmp_path)
    assert changed_input.value.status_code == 409


def test_disease_context_is_persisted_per_graph_build_and_hash_verified(tmp_path, db):
    import csv
    import hashlib

    project = get_erpos_project(db)
    run_id = f"ANALYSIS_RUN:KG_GRAPH_BUNDLE:{uuid4()}"
    db.add(AnalysisRun(analysis_run_id=run_id, project_id=project.project_id, run_type="kg_graph_bundle", status="completed", parameters={}, software_versions={}, input_hashes={}))
    build_id = f"GRAPH_BUILD:{project.project_id}:{uuid4().hex[:16]}"
    build = GraphBuild(graph_build_id=build_id, project_id=project.project_id, analysis_run_id=run_id, build_key=uuid4().hex, status="materialized", graph_source="neo4j", input_hashes={}, summary_json={})
    db.add(build)
    db.commit()
    path = tmp_path / "subgraph_nodes.csv"
    properties = '{"project_id":"%s","disease_id":"MONDO:0007254","s1_analysis_run_id":"S1:test"}' % project.project_id
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["id", "type", "label", "properties_json"])
        writer.writeheader()
        writer.writerow({"id": "DISEASE_CONTEXT:test", "type": "DiseaseContext", "label": "ER positive", "properties_json": properties})
        writer.writerow({"id": "MONDO:0007254", "type": "Disease", "label": "Breast cancer", "properties_json": "{}"})
    build.input_hashes = {"subgraph_nodes.csv": hashlib.sha256(path.read_bytes()).hexdigest()}
    db.commit()

    assert _persist_disease_contexts(db, build, tmp_path) == 1
    assert _persist_disease_contexts(db, build, tmp_path) == 1
    saved = db.scalar(select(DiseaseContext).where(DiseaseContext.graph_build_id == build_id))
    assert saved.context_json["s1_analysis_run_id"] == "S1:test"
    assert saved.disease_id == "MONDO:0007254" and saved.context_label == "ER positive"
    assert db.scalar(select(func.count()).select_from(DiseaseContext).where(DiseaseContext.graph_build_id == build_id)) == 1

    path.write_text(path.read_text(encoding="utf-8").replace("ER positive", "changed"), encoding="utf-8")
    with pytest.raises(HTTPException) as changed_input:
        _persist_disease_contexts(db, build, tmp_path)
    assert changed_input.value.status_code == 409


def test_compare_s2_runs_matches_only_exact_condition_and_perturbagen(db):
    project = get_erpos_project(db)
    run_a_id = f"ANALYSIS_RUN:S2_LINCS:{uuid4()}"
    run_b_id = f"ANALYSIS_RUN:S2_LINCS:{uuid4()}"
    db.add_all([
        AnalysisRun(analysis_run_id=run_a_id, project_id=project.project_id, run_type="s2_lincs", status="completed", parameters={"condition_count": 2}, software_versions={"pipeline": "1"}, input_hashes={"s1": "a"}),
        AnalysisRun(analysis_run_id=run_b_id, project_id=project.project_id, run_type="s2_lincs", status="completed", parameters={"condition_count": 2}, software_versions={"pipeline": "2"}, input_hashes={"s1": "b"}),
    ])
    db.flush()
    def add_result(run_id, result_id, key, pert_id, wtcs, rank):
        db.add(S2ConditionResult(result_id=result_id, project_id=project.project_id, analysis_run_id=run_id, pert_id=pert_id, condition_key=key, rank_within_condition=rank, condition_json={"WTCS_median": wtcs, "rank_within_condition": rank}))
    add_result(run_a_id, f"S2_CONDITION:{uuid4()}", "MCF7|trt_cp|0.1|24 h", "BRD-A", -0.2, 1)
    add_result(run_a_id, f"S2_CONDITION:{uuid4()}", "MCF7|trt_cp|1.0|24 h", "BRD-A", -0.3, 2)
    add_result(run_b_id, f"S2_CONDITION:{uuid4()}", "MCF7|trt_cp|0.1|24 h", "BRD-A", -0.5, 3)
    add_result(run_b_id, f"S2_CONDITION:{uuid4()}", "MCF7|trt_cp|0.1|24 h", "BRD-B", -0.8, 1)
    db.commit()

    result = compare_project_runs(project.project_id, run_a_id, run_b_id, db)
    assert result["run_type"] == "s2_lincs"
    assert result["provenance_comparison"]["identical_input_hashes"] is False
    assert result["provenance_comparison"]["different_software_versions"] == {"pipeline": {"run_a": "1", "run_b": "2"}}
    comparison = result["output_comparison"]["condition_matched_comparison"]
    assert comparison["shared_condition_perturbagen_rows"] == 1
    assert comparison["only_in_run_a"] == 1 and comparison["only_in_run_b"] == 1
    assert comparison["wtcs_delta_run_b_minus_run_a"]["median"] == pytest.approx(-0.3)
    assert "never pooled" in comparison["comparison_unit"]
    with pytest.raises(HTTPException) as same_run:
        compare_project_runs(project.project_id, run_a_id, run_a_id, db)
    assert same_run.value.status_code == 422
    other = create_project(ProjectCreate(slug=f"compare-isolation-{uuid4().hex[:8]}", name="运行比较隔离测试", disease_id="MONDO:0000001"), db)
    with pytest.raises(HTTPException) as cross_project:
        compare_project_runs(other.project_id, run_a_id, run_b_id, db)
    assert cross_project.value.status_code == 404


def test_job_status_uses_analysis_run_without_claiming_async_progress(db):
    project = get_erpos_project(db)
    run_id = f"ANALYSIS_RUN:S1_PRECOMPUTED:{uuid4()}"
    db.add(AnalysisRun(analysis_run_id=run_id, project_id=project.project_id, run_type="s1_precomputed_deg", status="completed", parameters={"significant_genes": 12}))
    db.commit()
    status = job_status(run_id, db)
    assert status["job_id"] == status["analysis_run_id"] == run_id
    assert status["project_id"] == project.project_id
    assert status["state"] == "completed" and status["progress"] == 100
    assert "任务队列未启用" in status["description"]
    with pytest.raises(HTTPException) as missing:
        job_status(f"missing-{uuid4()}", db)
    assert missing.value.status_code == 404


def test_test_only_project_status_is_explicit_and_persisted(db):
    default_project = create_project(
        ProjectCreate(
            slug=f"active-default-{uuid4().hex[:8]}",
            name="默认正式项目",
            disease_id="MONDO:0000001",
        ),
        db,
    )
    assert default_project.status == "active"

    project = create_project(
        ProjectCreate(
            slug=f"test-only-{uuid4().hex[:10]}",
            name="隔离测试项目",
            disease_id="MONDO:0000001",
            status="test_only",
        ),
        db,
    )
    assert project.status == "test_only"
    assert db.get(Project, project.project_id).status == "test_only"
    with pytest.raises(ValidationError):
        ProjectCreate(
            slug=f"invalid-status-{uuid4().hex[:8]}",
            name="无效状态项目",
            disease_id="MONDO:0000001",
            status="testing",
        )


def test_project_studies_are_idempotent_and_dataset_links_are_project_scoped(db):
    project = create_project(ProjectCreate(slug=f"study-owner-{uuid4().hex[:8]}", name="Study owner", disease_id="MONDO:0000001"), db)
    other = create_project(ProjectCreate(slug=f"study-other-{uuid4().hex[:8]}", name="Other project", disease_id="MONDO:0000002"), db)
    payload = StudyCreate(accession="gse123456", title="Example cohort", design_json={"case": "Tumor", "control": "Normal"})
    with pytest.raises(ValidationError):
        StudyCreate(accession="   ", title="Blank accession")

    study = create_study(project.project_id, payload, db)
    repeated = create_study(project.project_id, payload.model_copy(update={"accession": "GSE123456"}), db)
    assert study.study_id == repeated.study_id
    assert study.accession == "GSE123456"
    assert [row.study_id for row in list_studies(project.project_id, db)] == [study.study_id]
    with pytest.raises(HTTPException) as conflict:
        create_study(project.project_id, payload.model_copy(update={"title": "Different cohort"}), db)
    assert conflict.value.status_code == 409

    dataset = register_dataset(DatasetCreate(project_id=project.project_id, study_id=study.study_id, source="GEO", release="GSE123456"), db)
    assert dataset.study_id == study.study_id
    listed_dataset = next(item for item in list_datasets(project.project_id, db)["items"] if item["dataset_id"] == dataset.dataset_id)
    assert listed_dataset["study_id"] == study.study_id
    assert listed_dataset["study"] == {"accession": study.accession, "title": study.title}
    unlinked = Dataset(project_id=project.project_id, source="GEO", release="legacy", sha256="a" * 64, state="validated", metadata_json={})
    db.add(unlinked); db.commit(); db.refresh(unlinked)
    linked = link_dataset_to_study(project.project_id, unlinked.dataset_id, DatasetStudyLink(study_id=study.study_id), db)
    assert linked.study_id == study.study_id
    with pytest.raises(HTTPException) as wrong_project:
        register_dataset(DatasetCreate(project_id=other.project_id, study_id=study.study_id, source="GEO"), db)
    assert wrong_project.value.status_code == 422
    with pytest.raises(HTTPException) as cross_project_link:
        link_dataset_to_study(other.project_id, unlinked.dataset_id, DatasetStudyLink(study_id=study.study_id), db)
    assert cross_project_link.value.status_code == 404


def test_http_project_creation_and_upload_remains_pending(tmp_path, monkeypatch):
    from app.core.config import settings
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(settings, "data_upload_dir", str(upload_root))
    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(test_engine)
    test_session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)

    def override_db():
        with test_session() as session:
            yield session

    original_overrides = fastapi_app.dependency_overrides.copy()
    fastapi_app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(fastapi_app)
        created = client.post("/api/v1/projects", json={"slug": f"http-upload-{uuid4().hex[:8]}", "name": "HTTP 上传闭环测试", "disease_id": "MONDO:0000001"})
        assert created.status_code == 201
        project_id = created.json()["project_id"]
        study_response = client.post(f"/api/v1/projects/{project_id}/studies", json={"accession": "gse765432", "title": "上传关联测试研究"})
        assert study_response.status_code == 201, study_response.text
        study_id = study_response.json()["study_id"]
        assert client.get(f"/api/v1/projects/{project_id}/studies").json()[0]["study_id"] == study_id

        content = b"gene_symbol,logFC,adj.P.Val,t\nESR1,1.2,0.001,4.5\n"
        uploaded = client.post(
            f"/api/v1/projects/{project_id}/datasets/upload",
            params={"filename": "deg.csv", "dataset_type": "precomputed_deg", "study_id": study_id},
            content=content,
            headers={"Content-Type": "text/csv"},
        )
        assert uploaded.status_code == 201, uploaded.text
        body = uploaded.json()
        assert body["state"] == "validation_pending"
        assert body["analysis_started"] is False
        assert body["study_id"] == study_id
        assert body["sha256"]
        assert body["validation"]["row_count"] == 1
        stored = upload_root / project_id / body["dataset_id"] / "data.csv"
        assert stored.read_bytes() == content
        with test_session() as session:
            dataset = list_datasets(project_id, session)["items"][0]
            assert dataset["state"] == "validation_pending" and dataset["study_id"] == study_id
    finally:
        fastapi_app.dependency_overrides.clear()
        fastapi_app.dependency_overrides.update(original_overrides)
        Base.metadata.drop_all(test_engine)
        test_engine.dispose()


def test_s1_gene_table_and_volcano_use_existing_limma_results(db):
    project = get_erpos_project(db)
    up = s1_genes(project.project_id, direction="up", limit=20, offset=0, significant_only=True, db=db)
    down = s1_genes(project.project_id, direction="down", limit=20, offset=0, significant_only=True, db=db)
    volcano = s1_volcano(project.project_id, db)

    assert up["available"] is True and up["total"] == 3422 and len(up["items"]) == 20
    assert down["available"] is True and down["total"] == 3644 and len(down["items"]) == 20
    assert all(row["direction"] == "up" and row["adj.P.Val"] < 0.05 for row in up["items"])
    assert all(row["direction"] == "down" and row["adj.P.Val"] < 0.05 for row in down["items"])
    assert volcano["total"] == 21655 and volcano["fdr_threshold"] == 0.05
    assert sum(point["direction"] == "up" for point in volcano["points"]) == 3422
    assert sum(point["direction"] == "down" for point in volcano["points"]) == 3644


def test_external_s1_validation_is_hash_verified_and_project_scoped(db):
    er_project = get_erpos_project(db)
    page = s1_external_validation(er_project.project_id, limit=2, offset=0, db=db)
    assert page.available is True
    assert page.source_dataset == "GSE139038" and page.validation_run == "Exp010"
    assert page.sample_design["paired_ER_positive_patients"] == 14
    assert page.summary["n_common_genes"] == 13973
    assert page.total_conditions == 3715 and len(page.items) == 2
    assert page.items[0].pert_id == "BRD-A00147595"
    assert len(page.provenance["ranking_sha256"]) == 64
    next_condition = s1_external_validation(er_project.project_id, limit=2, offset=2, db=db).items[0]
    assert (next_condition.pert_id, next_condition.dose, next_condition.time) != (page.items[0].pert_id, page.items[0].dose, page.items[0].time)

    other = create_project(ProjectCreate(slug=f"external-{uuid4().hex[:10]}", name="无外部队列项目", disease_id="MONDO:0000001"), db)
    unavailable = s1_external_validation(other.project_id, limit=10, offset=0, db=db)
    assert unavailable.available is False and unavailable.items == []


def test_subgraph_is_bounded_and_source_evidence_is_queryable(db):
    project = get_erpos_project(db)
    summary = graph_summary(project.project_id, db)
    assert summary["node_count"] > 6000 and summary["edge_count"] > 8000
    page = subgraph(project.project_id, node_id=None, max_depth=2, limit=500, cursor=0, db=db)
    assert len(page["nodes"]) == 200
    assert page["truncated"] is True
    routes = drug_mechanisms(project.project_id, "BRD-K44227013", db)["routes"]
    assert routes
    evidence_id = routes[0]["evidence_ids"][0]
    from app.graph_repository import get_evidence
    assert get_evidence(evidence_id)["source_release"] == "OptimusKG 3.2"
    s1_routes = [route for route in routes if route["route_type"] == "s1_enrichment_overlap"]
    assert s1_routes
    assert s1_routes[0]["candidate_id"].startswith("CANDIDATE:")
    assert s1_routes[0]["disease_context_id"] == "DISEASE_CONTEXT:GSE42568_ERPOS_TUMOR_VS_NORMAL"
    assert len(s1_routes[0]["evidence_ids"]) == 4
    assert all(get_evidence(item) for item in s1_routes[0]["evidence_ids"])


def test_s1_enrichment_extension_is_provenanced_and_available_in_project_graph(db):
    from app.graph_repository import get_evidence, load_csv_graph

    project = get_erpos_project(db)
    nodes, edges = load_csv_graph()
    pathways = [node for node in nodes.values() if node["type"] in {"Pathway", "GeneSet"} and node["properties"].get("analysis_run_id") == "ANALYSIS_RUN:S1_PATHWAY_ENRICHMENT_20261112"]
    enriched_edges = [edge for edge in edges if edge["label"] in {"S1-ENRICHED-PATHWAY", "S1-ENRICHED-GENE"}]
    assert len(pathways) == 738
    assert len(enriched_edges) == 30179
    evidence = get_evidence(enriched_edges[0]["id"])
    assert evidence["source_release"].startswith("GSE42568 S1 pathway enrichment")
    assert evidence["source_assertion"]["analysis_run_id"] == "ANALYSIS_RUN:S1_PATHWAY_ENRICHMENT_20261112"
    summary = graph_summary(project.project_id, db)
    assert summary["nodes_by_type"]["GeneSet"] > 0


def test_project_s1_pathway_enrichment_run_is_scoped_and_queryable(db, tmp_path, monkeypatch):
    import hashlib
    from pathlib import Path
    import app.main as main_module

    project = create_project(ProjectCreate(slug=f"enrich-{uuid4().hex[:10]}", name="项目级富集测试", disease_id="MONDO:0000001"), db)
    s1_path = tmp_path / "limma_all_genes.csv"
    s1_path.write_text("gene_id,logFC,adj.P.Val,t\nESR1,1,0.01,4\nGAPDH,-1,0.02,-3\n", encoding="utf-8")
    digest = hashlib.sha256(s1_path.read_bytes()).hexdigest()
    s1 = AnalysisRun(analysis_run_id=f"ANALYSIS_RUN:S1_PRECOMPUTED:{uuid4()}", project_id=project.project_id, run_type="s1_precomputed_deg", status="completed", parameters={"contrast": "Case - Control", "output_sha256": digest}, input_hashes={})
    db.add(s1); db.commit()
    monkeypatch.setattr(main_module, "_precomputed_s1_path", lambda project_id, run: s1_path)

    def fake_enrichment(repo_root, input_path, output_dir, identifier_type, contrast, fdr_threshold):
        output_dir.mkdir(parents=True, exist_ok=True)
        contents = {
            "gene_mapping_audit.csv": "gene_id,ENTREZID,mapping_status\nESR1,2099,mapped_one_to_one\n",
            "s1_up_genes.csv": "gene_id,ENTREZID\nESR1,2099\n", "s1_down_genes.csv": "gene_id,ENTREZID\nGAPDH,2597\n",
            "go_up.csv": "ID,Description,p.adjust,significant\nGO:0000001,test term,0.01,TRUE\n", "go_down.csv": "ID,Description,p.adjust,significant\n",
            "reactome_up.csv": "ID,Description,p.adjust,significant\n", "reactome_down.csv": "ID,Description,p.adjust,significant\n",
            "hallmark_gsea.csv": "pathway,NES,padj\n", "pathway_gene_links.csv": "analysis,pathway,ENTREZID\n", "summary.json": "{}", "enrichment_report.md": "# 测试\n",
        }
        for name, content in contents.items(): (output_dir / name).write_text(content, encoding="utf-8")
        (output_dir / "figures").mkdir(exist_ok=True)
        return {"input_genes": 2, "mapped_universe": 2, "up_genes": 1, "down_genes": 1, "go_up_significant": 1, "go_down_significant": 0, "reactome_up_significant": 0, "reactome_down_significant": 0, "hallmark_significant": 0, "r_version": "test", "packages": {}}

    monkeypatch.setattr(main_module, "run_project_s1_enrichment", fake_enrichment)
    response = run_project_pathway_enrichment(project.project_id, S1EnrichmentRequest(s1_analysis_run_id=s1.analysis_run_id), db)
    assert response["status"] == "completed"
    run = db.get(AnalysisRun, response["analysis_run_id"])
    assert run.project_id == project.project_id and run.status == "completed"
    assert run.parameters["significant_pathway_counts"]["go_up"] == 1
    page = s1_pathways(project.project_id, "go_up", 10, 0, True, None, db)
    assert page["analysis_run_id"] == run.analysis_run_id
    assert page["items"][0]["ID"] == "GO:0000001"
    assert s1_summary(project.project_id, db)["pathway_enrichment_available"] is True
    newer_s1 = AnalysisRun(analysis_run_id=f"ANALYSIS_RUN:S1_PRECOMPUTED:{uuid4()}", project_id=project.project_id, run_type="s1_precomputed_deg", status="completed", parameters={"contrast": "Case - Control", "output_sha256": digest}, input_hashes={})
    db.add(newer_s1); db.commit()
    assert s1_summary(project.project_id, db)["pathway_enrichment_available"] is False
    latest_pathways = s1_pathways(project.project_id, "go_up", 10, 0, True, None, db)
    assert latest_pathways["available"] is False
    previous_pathways = s1_pathways(project.project_id, "go_up", 10, 0, True, run.analysis_run_id, db)
    assert previous_pathways["items"][0]["ID"] == "GO:0000001"
    result_dir = (Path(__file__).resolve().parents[2] / response["result_dir"]).resolve()
    allowed_project_dir = (Path(__file__).resolve().parents[2] / "data" / "processed" / "projects" / project.project_id).resolve()
    assert result_dir.is_relative_to(allowed_project_dir)
    for child in sorted(result_dir.rglob("*"), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    result_dir.rmdir()


def test_dataset_registration_starts_pending_and_project_scope_is_enforced(db):
    project = get_erpos_project(db)
    dataset = register_dataset(DatasetCreate(project_id=project.project_id, source="test-upload", release="local"), db)
    assert dataset.state == "validation_pending"
    assert any(row["state"] == "validation_pending" for row in list_datasets(project.project_id, db)["items"])
    with pytest.raises(HTTPException) as exc:
        list_runs("missing-project", db)
    assert exc.value.status_code == 404


def test_precomputed_s1_run_is_scoped_to_project_and_records_provenance(db):
    import hashlib
    import shutil
    from pathlib import Path

    project = create_project(ProjectCreate(slug=f"test-s1-{uuid4().hex[:10]}", name="预计算 S1 项目隔离测试", disease_id="MONDO:0000001"), db)
    dataset_id = str(uuid4())
    root = Path(__file__).resolve().parents[2]
    upload_dir = root / "data" / "raw" / "uploads" / project.project_id / dataset_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    source = upload_dir / "deg.csv"
    source.write_text("gene_symbol,logFC,adj.P.Val,t\nG1,1.5,0.01,4\nG2,-2.0,0.2,-3\n", encoding="utf-8")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    dataset = Dataset(dataset_id=dataset_id, project_id=project.project_id, source="user_upload", release="test", sha256=source_hash, license_status="pending_review", state="validation_pending", metadata_json={"dataset_type": "precomputed_deg", "storage_path": source.relative_to(root).as_posix(), "upload_validated": True})
    db.add(dataset)
    db.commit()
    try:
        result = run_precomputed_s1(project.project_id, dataset_id, PrecomputedS1Request(positive_group="Case", negative_group="Control"), db)
        assert result["status"] == "completed" and result["dataset_state"] == "validated"
        assert result["summary"]["input_sha256"] == source_hash
        assert result["summary"]["up"] == 1 and result["summary"]["down"] == 0
        assert s1_summary(project.project_id, db)["available"] is True
        assert s1_summary(project.project_id, db)["contrast"] == "Case - Control"
        assert s1_genes(project.project_id, direction="up", limit=50, offset=0, significant_only=True, db=db)["items"][0]["gene_symbol"] == "G1"
        historical = get_erpos_project(db)
        assert s1_summary(historical.project_id, db)["tested_genes"] == 21655
    finally:
        shutil.rmtree(upload_dir.parent.parent, ignore_errors=True)
        shutil.rmtree(root / "data" / "processed" / "projects" / project.project_id, ignore_errors=True)


def test_precomputed_s1_rejects_unvalidated_dataset_metadata(db):
    project = create_project(ProjectCreate(slug=f"test-s1-gate-{uuid4().hex[:8]}", name="S1 上传校验门禁测试", disease_id="MONDO:0000001"), db)
    dataset_id = str(uuid4())
    db.add(Dataset(dataset_id=dataset_id, project_id=project.project_id, source="manual", release="test", sha256="", license_status="pending_review", state="validation_pending", metadata_json={"dataset_type": "precomputed_deg", "storage_path": "data/raw/uploads/fake.csv"}))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        run_precomputed_s1(project.project_id, dataset_id, PrecomputedS1Request(positive_group="Case", negative_group="Control"), db)
    assert exc.value.status_code == 409


def test_project_slug_is_unique(db):
    with pytest.raises(ValidationError):
        ProjectCreate(slug=f"missing-disease-{uuid4().hex[:8]}", name="Disease ID required")
    payload = ProjectCreate(slug=f"test-{uuid4().hex[:12]}", name="Test project", disease_id="MONDO:0000001")
    created = create_project(payload, db)
    assert created.slug == payload.slug
    with pytest.raises(HTTPException) as exc:
        create_project(payload, db)
    assert exc.value.status_code == 409


def test_graph_build_snapshot_is_versioned_and_idempotent(db):
    project = get_erpos_project(db)
    first = graph_build(project.project_id, db)
    second = graph_build(project.project_id, db)
    listed = list_graph_builds(project.project_id, db)

    assert first["status"] == "materialized_snapshot"
    assert first["graph_build_id"] == second["graph_build_id"]
    assert first["input_hashes"] and all(len(value) == 64 for value in first["input_hashes"].values())
    assert first["summary"]["snapshot_kind"] == "已验证项目图谱输入包"
    assert "幂等导入" in first["detail"]
    assert len(listed["items"]) == 1


def test_graph_build_rejects_project_without_audited_graph_inputs(db):
    project = create_project(ProjectCreate(slug=f"test-no-graph-{uuid4().hex[:8]}", name="无图谱输入测试", disease_id="MONDO:0000001"), db)
    with pytest.raises(HTTPException) as exc:
        graph_build(project.project_id, db)
    assert exc.value.status_code == 409
    assert list_graph_builds(project.project_id, db)["items"] == []


def test_graph_jobs_prefer_the_isolated_kg_virtual_environment(tmp_path):
    from app.main import _resolve_kg_python

    kg_python = tmp_path / "kg" / ".venv" / "Scripts" / "python.exe"
    s2_python = tmp_path / "s2_lincs" / ".venv" / "Scripts" / "python.exe"
    kg_python.parent.mkdir(parents=True)
    s2_python.parent.mkdir(parents=True)
    kg_python.write_bytes(b"")
    s2_python.write_bytes(b"")

    assert _resolve_kg_python(tmp_path) == kg_python
    kg_python.unlink()
    assert _resolve_kg_python(tmp_path) == s2_python


def test_graph_build_inputs_selects_and_hash_verifies_project_bundle(tmp_path, db):
    import hashlib
    import json

    project = create_project(ProjectCreate(slug=f"test-bundle-{uuid4().hex[:8]}", name="GraphBundle 读取测试", disease_id="MONDO:0000001"), db)
    run_id = f"ANALYSIS_RUN:KG_GRAPH_BUNDLE:{uuid4()}"
    bundle_dir = tmp_path / "data" / "processed" / "projects" / project.project_id / "graph_bundle" / "runs" / run_id.rsplit(":", 1)[-1]
    bundle_dir.mkdir(parents=True)
    outputs = {"subgraph_nodes.csv": b"id,type,label,properties_json\n", "subgraph_edges.csv": b"from,label,to,relation,source_release,undirected,properties_json\n"}
    hashes = {}
    for name, content in outputs.items():
        (bundle_dir / name).write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    manifest = {"status": "validated", "project_id": project.project_id, "analysis_run_id": run_id, "test_only": False, "output_hashes": hashes}
    manifest_path = bundle_dir / "bundle_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (bundle_dir / "bundle_checksums.json").write_text(json.dumps({**hashes, "bundle_manifest.json": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}), encoding="utf-8")
    run_parameters = {"manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(), "output_hashes": hashes}
    run_software = {}
    run_inputs = {}
    analysis_manifest_path = bundle_dir / "analysis_run_manifest.json"
    analysis_manifest_path.write_text(json.dumps({"analysis_run_id": run_id, "project_id": project.project_id, "run_type": "kg_graph_bundle", "status": "completed", "parameters": run_parameters, "software_versions": run_software, "input_hashes": run_inputs}), encoding="utf-8")
    run_parameters["analysis_run_manifest_sha256"] = hashlib.sha256(analysis_manifest_path.read_bytes()).hexdigest()
    run = AnalysisRun(analysis_run_id=run_id, project_id=project.project_id, run_type="kg_graph_bundle", status="completed", parameters=run_parameters, software_versions=run_software, input_hashes=run_inputs)
    db.add(run); db.commit()

    resolved_run, subgraph_dir, extension_dir, input_hashes, test_only = _graph_build_inputs(project, tmp_path, db, run_id)
    assert resolved_run.analysis_run_id == run_id
    assert subgraph_dir == bundle_dir and extension_dir is None and not test_only
    assert set(input_hashes) == {"bundle_manifest.json", "bundle_checksums.json", "analysis_run_manifest.json", *hashes.keys()}

    original_nodes = (bundle_dir / "subgraph_nodes.csv").read_bytes()
    (bundle_dir / "subgraph_nodes.csv").write_text("tampered", encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        _graph_build_inputs(project, tmp_path, db, run_id)
    assert exc.value.status_code == 409

    (bundle_dir / "subgraph_nodes.csv").write_bytes(original_nodes)
    manifest["test_only"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (bundle_dir / "bundle_checksums.json").write_text(json.dumps({**hashes, "bundle_manifest.json": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}), encoding="utf-8")
    with pytest.raises(HTTPException, match="AnalysisRun"):
        _graph_build_inputs(project, tmp_path, db, run_id)


def test_project_csv_graph_api_queries_are_scoped_to_selected_graph_build(tmp_path, db, monkeypatch):
    import csv
    import json
    from app import main as main_module
    from app.core.config import settings

    project = create_project(ProjectCreate(slug=f"test-graph-api-{uuid4().hex[:8]}", name="项目图查询 API 测试", disease_id="MONDO:0000001"), db)
    other = create_project(ProjectCreate(slug=f"test-graph-api-other-{uuid4().hex[:8]}", name="另一个项目", disease_id="MONDO:0000002"), db)
    run_id = f"ANALYSIS_RUN:KG_GRAPH_BUNDLE:{uuid4()}"
    db.add(AnalysisRun(analysis_run_id=run_id, project_id=project.project_id, run_type="kg_graph_bundle", status="completed", parameters={}, software_versions={}, input_hashes={}))
    build_id = f"GRAPH_BUILD:{project.project_id}:csv-test"
    db.add(GraphBuild(graph_build_id=build_id, project_id=project.project_id, analysis_run_id=run_id, build_key=uuid4().hex.ljust(64, "0"), status="materialized_snapshot", graph_source="csv", input_hashes={}, summary_json={}))
    db.commit()

    nodes = [
        {"id": "CTX:test", "type": "DiseaseContext", "label": "疾病上下文", "properties_json": json.dumps({"project_id": project.project_id, "project_slug": project.slug, "analysis_run_id": run_id})},
        {"id": "CANDIDATE:test:BRD-TEST", "type": "Candidate", "label": "Drug A", "properties_json": json.dumps({"project_id": project.project_id, "project_slug": project.slug, "pert_id": "BRD-TEST"})},
        {"id": "CHEMBL1", "type": "Drug", "label": "Drug A", "properties_json": json.dumps({"inchi_key": "TEST-INCHI"})},
        {"id": "ENSG000001", "type": "Gene", "label": "TP53", "properties_json": json.dumps({"symbol": "TP53"})},
        {"id": "GO:0000001", "type": "Pathway", "label": "Test pathway", "properties_json": json.dumps({"name": "Test pathway"})},
        {"id": "S2_CONDITION:test:1", "type": "S2Condition", "label": "1 uM / 24 h", "properties_json": json.dumps({"dose": "1 uM", "time": "24 h"})},
    ]
    edges = [
        {"from": "CTX:test", "label": "PROJECT-HAS_CANDIDATE", "to": "CANDIDATE:test:BRD-TEST", "relation": "HAS_CANDIDATE", "source_release": "S2 test", "undirected": "false", "properties_json": json.dumps({"analysis_run_id": "RUN:S2:test"})},
        {"from": "CANDIDATE:test:BRD-TEST", "label": "PROJECT-REFERENCES_DRUG", "to": "CHEMBL1", "relation": "REFERENCES_DRUG", "source_release": "LINCS test", "undirected": "false", "properties_json": json.dumps({"analysis_run_id": "RUN:S2:test"})},
        {"from": "CHEMBL1", "label": "DRG-GEN", "to": "ENSG000001", "relation": "TARGETS", "source_release": "Public KG test", "undirected": "false", "properties_json": json.dumps({"source": "test"})},
        {"from": "GO:0000001", "label": "PWY-GEN", "to": "ENSG000001", "relation": "CONTAINS_GENE", "source_release": "Public KG test", "undirected": "false", "properties_json": json.dumps({"source": "test"})},
        {"from": "CTX:test", "label": "S1-ENRICHED-PATHWAY", "to": "GO:0000001", "relation": "HAS_ENRICHED_PATHWAY", "source_release": "S1 test", "undirected": "false", "properties_json": json.dumps({"analysis_assertions": [{"analysis_run_id": "RUN:ENRICH:test"}]})},
        {"from": "GO:0000001", "label": "S1-ENRICHED-GENE", "to": "ENSG000001", "relation": "HAS_ENRICHED_GENE", "source_release": "S1 test", "undirected": "false", "properties_json": json.dumps({"analysis_assertions": [{"analysis_run_id": "RUN:ENRICH:test"}]})},
        {"from": "CANDIDATE:test:BRD-TEST", "label": "PROJECT-HAS_S2_CONDITION", "to": "S2_CONDITION:test:1", "relation": "HAS_S2_CONDITION", "source_release": "S2 test", "undirected": "false", "properties_json": json.dumps({"analysis_run_id": "RUN:S2:test"})},
    ]
    for filename, rows, fields in [
        ("subgraph_nodes.csv", nodes, ["id", "type", "label", "properties_json"]),
        ("subgraph_edges.csv", edges, ["from", "label", "to", "relation", "source_release", "undirected", "properties_json"]),
    ]:
        with (tmp_path / filename).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    monkeypatch.setattr(settings, "graph_source", "csv")
    monkeypatch.setattr(main_module, "_graph_bundle_dir_for_build", lambda *_: str(tmp_path))

    counts = graph_summary(project.project_id, db, build_id)
    found = graph_search(project.project_id, "TP53", 20, db, build_id)
    graph_page = subgraph(project.project_id, None, 2, 100, 0, db, build_id)
    mechanism = drug_mechanisms(project.project_id, "BRD-TEST", db, build_id)
    evidence_id = __import__("hashlib").sha256("CHEMBL1|DRG-GEN|ENSG000001|Public KG test".encode()).hexdigest()
    source = evidence(evidence_id, db, build_id)

    assert counts["node_count"] == 6 and counts["edge_count"] == 7
    assert found["items"] == [{"id": "ENSG000001", "type": "Gene", "label": "TP53"}]
    assert graph_page["analysis_run_id"] == run_id and len(graph_page["nodes"]) == 6
    assert {row["route_type"] for row in mechanism["routes"]} == {"public_kg_pathway", "s1_enrichment_overlap"}
    assert source["source_release"] == "Public KG test"
    with pytest.raises(HTTPException) as exc:
        graph_summary(other.project_id, db, build_id)
    assert exc.value.status_code == 404


def test_project_graph_queries_resolve_and_verify_immutable_bundle_path(tmp_path, db, monkeypatch):
    import hashlib
    import json
    from app.core.config import settings

    monkeypatch.setattr(settings, "graph_source", "csv")
    project = create_project(ProjectCreate(slug=f"test-graph-path-{uuid4().hex[:8]}", name="图包路径校验", disease_id="MONDO:0000001"), db)
    run_id = f"ANALYSIS_RUN:KG_GRAPH_BUNDLE:{uuid4()}"
    token = run_id.rsplit(":", 1)[-1]
    relative_dir = Path("data") / "processed" / "projects" / project.project_id / "graph_bundle" / "runs" / token
    bundle_dir = tmp_path / relative_dir
    bundle_dir.mkdir(parents=True)
    outputs = {"subgraph_nodes.csv": b"id,type,label,properties_json\n", "subgraph_edges.csv": b"from,label,to,relation,source_release,undirected,properties_json\n"}
    output_hashes = {}
    for filename, content in outputs.items():
        (bundle_dir / filename).write_bytes(content)
        output_hashes[filename] = hashlib.sha256(content).hexdigest()
    manifest = {"status": "validated", "project_id": project.project_id, "analysis_run_id": run_id, "test_only": False, "output_hashes": output_hashes}
    manifest_path = bundle_dir / "bundle_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    checksums = {**output_hashes, "bundle_manifest.json": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}
    (bundle_dir / "bundle_checksums.json").write_text(json.dumps(checksums), encoding="utf-8")
    run_parameters = {"bundle_dir": relative_dir.as_posix(), "manifest_sha256": checksums["bundle_manifest.json"], "output_hashes": output_hashes}
    software_versions = {"pipeline": "test"}
    run_inputs = {"source_sha256": "a" * 64}
    analysis_manifest_path = bundle_dir / "analysis_run_manifest.json"
    analysis_manifest_path.write_text(json.dumps({"analysis_run_id": run_id, "project_id": project.project_id, "run_type": "kg_graph_bundle", "status": "completed", "parameters": run_parameters, "software_versions": software_versions, "input_hashes": run_inputs}), encoding="utf-8")
    run_parameters["analysis_run_manifest_sha256"] = hashlib.sha256(analysis_manifest_path.read_bytes()).hexdigest()
    run = AnalysisRun(analysis_run_id=run_id, project_id=project.project_id, run_type="kg_graph_bundle", status="completed", parameters=run_parameters, software_versions=software_versions, input_hashes=run_inputs)
    db.add(run); db.flush()
    recorded_hashes = {filename: hashlib.sha256((bundle_dir / filename).read_bytes()).hexdigest() for filename in [*output_hashes, "bundle_manifest.json", "bundle_checksums.json", "analysis_run_manifest.json"]}
    build = GraphBuild(graph_build_id=f"GRAPH_BUILD:{project.project_id}:test-path", project_id=project.project_id, analysis_run_id=run_id, build_key=uuid4().hex.ljust(64, "0"), status="materialized_snapshot", graph_source="csv", input_hashes=recorded_hashes, summary_json={})
    db.add(build); db.commit()

    resolved = _graph_bundle_dir_for_build(db, build, tmp_path)
    assert Path(resolved) == bundle_dir.resolve()
    (bundle_dir / "subgraph_nodes.csv").write_text("modified", encoding="utf-8")
    with pytest.raises(HTTPException, match="哈希校验失败"):
        _graph_bundle_dir_for_build(db, build, tmp_path)

def test_graph_build_id_is_project_scoped_for_queries(db):
    er_project = get_erpos_project(db)
    build = graph_build(er_project.project_id, db)
    other = create_project(ProjectCreate(slug=f"test-build-scope-{uuid4().hex[:8]}", name="版本隔离测试", disease_id="MONDO:0000001"), db)
    with pytest.raises(HTTPException) as exc:
        graph_summary(other.project_id, db, build["graph_build_id"])
    assert exc.value.status_code == 404


def test_neo4j_graph_build_query_gate_and_status_transitions(db, monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app import main as main_module
    from app.core.config import settings

    project = create_project(ProjectCreate(slug=f"test-neo4j-gate-{uuid4().hex[:8]}", name="Neo4j 状态门禁", disease_id="MONDO:0000001"), db)
    run = AnalysisRun(analysis_run_id=f"ANALYSIS_RUN:LEGACY:{uuid4()}", project_id=project.project_id, run_type="legacy_phase_b", status="completed", parameters={}, software_versions={}, input_hashes={})
    db.add(run); db.flush()
    materialized_id = f"GRAPH_BUILD:{project.project_id}:materialized"
    importing_id = f"GRAPH_BUILD:{project.project_id}:importing"
    failed_id = f"GRAPH_BUILD:{project.project_id}:failed"
    db.add_all([
        GraphBuild(graph_build_id=materialized_id, project_id=project.project_id, analysis_run_id=run.analysis_run_id, build_key="a" * 64, status="materialized", graph_source="neo4j", input_hashes={}, summary_json={}, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        GraphBuild(graph_build_id=importing_id, project_id=project.project_id, analysis_run_id=run.analysis_run_id, build_key="b" * 64, status="importing", graph_source="neo4j", input_hashes={}, summary_json={}, created_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
        GraphBuild(graph_build_id=failed_id, project_id=project.project_id, analysis_run_id=run.analysis_run_id, build_key="c" * 64, status="failed", graph_source="neo4j", input_hashes={}, summary_json={}, created_at=datetime(2026, 1, 3, tzinfo=timezone.utc)),
    ])
    db.commit()

    assert _graph_build_queryable(db.get(GraphBuild, materialized_id))
    assert not _graph_build_queryable(db.get(GraphBuild, importing_id))
    assert not _graph_build_queryable(db.get(GraphBuild, failed_id))
    assert _resolve_graph_build(db, project.project_id, None).graph_build_id == materialized_id
    with pytest.raises(HTTPException) as importing_error:
        _resolve_graph_build(db, project.project_id, importing_id)
    assert importing_error.value.status_code == 409
    with pytest.raises(HTTPException) as failed_error:
        _resolve_graph_build(db, project.project_id, failed_id)
    assert failed_error.value.status_code == 409

    monkeypatch.setattr(settings, "graph_source", "neo4j")
    monkeypatch.setattr(main_module, "_graph_build_inputs", lambda *args, **kwargs: (run, tmp_path, None, {"subgraph_nodes.csv": "a" * 64}, False))
    monkeypatch.setattr(main_module, "_resolve_kg_python", lambda *_: Path(__import__("sys").executable))
    monkeypatch.setattr(main_module, "graph_counts", lambda *args, **kwargs: {"node_count": 1, "edge_count": 1, "nodes_by_type": {}, "source": "neo4j"})

    observed = {}
    def failing_run(*args, **kwargs):
        observed["status"] = db.scalar(select(GraphBuild).where(GraphBuild.project_id == retry_project.project_id).order_by(GraphBuild.created_at.desc())).status
        return SimpleNamespace(returncode=1, stdout="", stderr="preflight failed")
    monkeypatch.setattr(main_module.subprocess, "run", failing_run)

    retry_project = create_project(ProjectCreate(slug=f"test-neo4j-retry-{uuid4().hex[:8]}", name="Neo4j 重试", disease_id="MONDO:0000002"), db)
    retry_run = AnalysisRun(analysis_run_id=f"ANALYSIS_RUN:LEGACY:{uuid4()}", project_id=retry_project.project_id, run_type="legacy_phase_b", status="completed", parameters={}, software_versions={}, input_hashes={})
    db.add(retry_run); db.commit()
    monkeypatch.setattr(main_module, "_graph_build_inputs", lambda *args, **kwargs: (retry_run, tmp_path, None, {"subgraph_nodes.csv": "b" * 64}, False))

    with pytest.raises(HTTPException):
        graph_build(retry_project.project_id, db)
    failed_record = db.scalar(select(GraphBuild).where(GraphBuild.project_id == retry_project.project_id))
    assert failed_record.status == "failed"
    assert observed["status"] == "importing"

    observed.clear()
    def succeeding_run(*args, **kwargs):
        observed["status"] = db.get(GraphBuild, failed_record.graph_build_id).status
        return SimpleNamespace(returncode=0, stdout='{"status":"PASS"}', stderr="")
    monkeypatch.setattr(main_module.subprocess, "run", succeeding_run)

    result = graph_build(retry_project.project_id, db)
    assert result["status"] == "materialized"
    assert observed["status"] == "importing"
    assert _resolve_graph_build(db, retry_project.project_id, result["graph_build_id"]).status == "materialized"


def test_pending_graph_build_evidence_is_rejected(db):
    project = create_project(ProjectCreate(slug=f"test-pending-evidence-{uuid4().hex[:8]}", name="待导入证据", disease_id="MONDO:0000001"), db)
    run = AnalysisRun(analysis_run_id=f"ANALYSIS_RUN:LEGACY:{uuid4()}", project_id=project.project_id, run_type="legacy_phase_b", status="completed", parameters={}, software_versions={}, input_hashes={})
    db.add(run); db.flush()
    build_id = f"GRAPH_BUILD:{project.project_id}:pending"
    db.add(GraphBuild(graph_build_id=build_id, project_id=project.project_id, analysis_run_id=run.analysis_run_id, build_key="f" * 64, status="importing", graph_source="neo4j", input_hashes={}, summary_json={}))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        evidence("e" * 64, db, build_id)
    assert exc.value.status_code == 409


def test_failed_retry_does_not_downgrade_an_already_materialized_build(db):
    project = create_project(ProjectCreate(slug=f"test-no-downgrade-{uuid4().hex[:8]}", name="状态不回退", disease_id="MONDO:0000001"), db)
    run = AnalysisRun(analysis_run_id=f"ANALYSIS_RUN:LEGACY:{uuid4()}", project_id=project.project_id, run_type="legacy_phase_b", status="completed", parameters={}, software_versions={}, input_hashes={})
    db.add(run); db.flush()
    build_id = f"GRAPH_BUILD:{project.project_id}:materialized"
    build = GraphBuild(graph_build_id=build_id, project_id=project.project_id, analysis_run_id=run.analysis_run_id, build_key="e" * 64, status="materialized", graph_source="neo4j", input_hashes={}, summary_json={"neo4j_membership": "registered"})
    db.add(build); db.commit()

    _mark_graph_build_failed(db, build, "late failure from a duplicate request")
    assert db.get(GraphBuild, build_id).status == "materialized"


def test_csv_materialized_snapshot_semantics_are_preserved(db):
    project = create_project(ProjectCreate(slug=f"test-csv-status-{uuid4().hex[:8]}", name="CSV 状态", disease_id="MONDO:0000001"), db)
    run = AnalysisRun(analysis_run_id=f"ANALYSIS_RUN:CSV:{uuid4()}", project_id=project.project_id, run_type="kg_graph_bundle", status="completed", parameters={}, software_versions={}, input_hashes={})
    db.add(run); db.flush()
    build_id = f"GRAPH_BUILD:{project.project_id}:csv"
    db.add(GraphBuild(graph_build_id=build_id, project_id=project.project_id, analysis_run_id=run.analysis_run_id, build_key="d" * 64, status="materialized_snapshot", graph_source="csv", input_hashes={}, summary_json={}))
    db.commit()

    assert _graph_build_queryable(db.get(GraphBuild, build_id))
    assert _resolve_graph_build(db, project.project_id, build_id).graph_build_id == build_id


def test_project_requires_standard_disease_curie():
    with pytest.raises(ValidationError):
        ProjectCreate(slug="bad-disease", name="Bad disease", disease_id="breast cancer")


def test_new_project_is_isolated_from_historical_erpos_data(db):
    projects = [
        create_project(ProjectCreate(slug=f"test-isolation-{uuid4().hex[:12]}", name=name, disease_id=disease_id), db)
        for name, disease_id in [("隔离测试项目甲", "MONDO:0000000"), ("隔离测试项目乙", "MONDO:0000001")]
    ]
    for project in projects:
        assert list_runs(project.project_id, db) == []
        assert s1_summary(project.project_id, db)["available"] is False
        assert s1_genes(project.project_id, direction="all", limit=5, offset=0, significant_only=True, db=db)["available"] is False
        assert s1_volcano(project.project_id, db)["available"] is False
        assert s1_pathways(project.project_id, collection="go_up", offset=0, db=db)["items"] == []
        assert candidates(project.project_id, offset=0, limit=50, db=db)["total"] == 0
        assert s2_evidence_matrix(project.project_id, offset=0, limit=50, db=db)["available"] is False
        assert graph_summary(project.project_id, db)["node_count"] == 0
        assert graph_summary(project.project_id, db)["edge_count"] == 0
        assert subgraph(project.project_id, node_id=None, max_depth=2, limit=100, cursor=0, db=db)["nodes"] == []
        assert drug_mechanisms(project.project_id, "BRD-K44227013", db)["routes"] == []

    assert projects[0].project_id != projects[1].project_id
    assert projects[0].disease_id != projects[1].disease_id

    historical = get_erpos_project(db)
    assert s1_summary(historical.project_id, db)["available"] is True
    pathway_summary_data = s1_summary(historical.project_id, db)
    assert pathway_summary_data["pathway_enrichment_available"] is True
    assert pathway_summary_data["pathway_counts"]["go_up"] == 206
    assert s1_pathways(historical.project_id, collection="go_up", limit=5, offset=0, db=db)["total"] == 206
    assert len(s1_pathways(historical.project_id, collection="go_up", limit=5, offset=0, db=db)["items"]) == 5
    hallmark = s1_pathways(historical.project_id, collection="hallmark_gsea", limit=100, offset=0, db=db)
    assert hallmark["total"] == 29
    assert any(row["NES"] > 0 for row in hallmark["items"])
    assert any(row["NES"] < 0 for row in hallmark["items"])
    assert candidates(historical.project_id, offset=0, limit=50, db=db)["total"] == 50
    matrix = s2_evidence_matrix(historical.project_id, offset=0, limit=10, db=db)
    assert matrix["available"] is True and matrix["total"] == 819
    assert matrix["items"][0]["pert_id"] == "BRD-K44227013"
    assert matrix["items"][0]["stability_grade"] == "low"
    assert matrix["items"][0]["external_s1_median_rank"]
    assert graph_summary(historical.project_id, db)["node_count"] > 6000
    matches = graph_search(historical.project_id, q="BRD-K44227013", limit=20, db=db)["items"]
    assert matches and "BRD-K44227013" in matches[0]["id"]
    assert graph_search(projects[0].project_id, q="BRD-K44227013", limit=20, db=db)["items"] == []


def test_project_s2_import_persists_condition_strata_and_is_idempotent(db):
    import csv
    import hashlib
    import json
    import shutil
    from pathlib import Path
    from uuid import uuid4

    project = create_project(ProjectCreate(slug=f"test-s2-db-{uuid4().hex[:10]}", name="S2 条件结果持久化验收", disease_id="MONDO:0000001"), db)
    root = Path(__file__).resolve().parents[2]
    s1_token = uuid4().hex
    s2_token = uuid4().hex
    s1_run_id = f"ANALYSIS_RUN:S1_LIMMA:{s1_token}"
    s2_run_id = f"ANALYSIS_RUN:S2_LINCS:{s2_token}"
    s1_dir = root / "data" / "processed" / "projects" / project.project_id / "s1" / s1_token
    s2_dir = root / "data" / "processed" / "projects" / project.project_id / "s2" / s2_token
    s1_dir.mkdir(parents=True, exist_ok=False)
    s2_dir.mkdir(parents=True, exist_ok=False)
    project_root = root / "data" / "processed" / "projects" / project.project_id
    try:
        s1_path = s1_dir / "limma_all_genes.csv"
        s1_path.write_text("gene_id,logFC,adj.P.Val,t\nG1,1,0.01,4\n", encoding="utf-8")
        s1_hash = hashlib.sha256(s1_path.read_bytes()).hexdigest()
        db.add(AnalysisRun(analysis_run_id=s1_run_id, project_id=project.project_id, run_type="s1_limma_microarray", status="completed", parameters={}, software_versions={}, input_hashes={}))
        db.commit()

        condition_path = s2_dir / "drug_condition_summary.csv"
        with condition_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["pert_id", "drug_name", "cell_id", "pert_type", "dose", "time", "n_signatures", "WTCS_median", "WTCS_mean", "WTCS_sd", "WTCS_mad", "quality_cc_median", "TAS_median", "replicates_median", "rank_within_condition"])
            writer.writeheader()
            writer.writerow({"pert_id":"BRD-TEST", "drug_name":"Test compound", "cell_id":"MCF7", "pert_type":"trt_cp", "dose":"1 uM", "time":"24 h", "n_signatures":"2", "WTCS_median":"-0.4", "WTCS_mean":"-0.3", "WTCS_sd":"0.1", "WTCS_mad":"0.05", "quality_cc_median":"0.7", "TAS_median":"0.5", "replicates_median":"3", "rank_within_condition":"1.5"})
            writer.writerow({"pert_id":"BRD-TEST", "drug_name":"Test compound", "cell_id":"MCF7", "pert_type":"trt_cp", "dose":"10 uM", "time":"24 h", "n_signatures":"1", "WTCS_median":"-0.2", "WTCS_mean":"-0.2", "WTCS_sd":"", "WTCS_mad":"0", "quality_cc_median":"0.8", "TAS_median":"0.6", "replicates_median":"3", "rank_within_condition":"2"})
        output_names = ["signature_scores.csv", "s1_gene_mapping_audit.csv", "qc_summary.json"]
        (s2_dir / "signature_scores.csv").write_text("sig_id,pert_id,WTCS\n", encoding="utf-8")
        (s2_dir / "s1_gene_mapping_audit.csv").write_text("gene_id,mapping_status\n", encoding="utf-8")
        (s2_dir / "qc_summary.json").write_text("{}", encoding="utf-8")
        outputs = {}
        for name in ["drug_condition_summary.csv", *output_names]:
            path = s2_dir / name
            outputs[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
        (s2_dir / "run_manifest.json").write_text(json.dumps({"analysis_run_id":s2_run_id, "project_id":project.project_id, "s1_analysis_run_id":s1_run_id, "s1_input_sha256":s1_hash, "status":"completed", "test_only":False, "software_versions":{"pipeline":"test"}, "inputs":{}, "outputs":outputs, "qc_summary":{"scored_signatures":3, "scored_drugs":1, "time":"24 h"}}, ensure_ascii=False), encoding="utf-8")

        imported = import_project_s2(project.project_id, S2ImportRequest(analysis_run_id=s2_run_id), db)
        assert imported["persisted_condition_count"] == 2
        page = project_s2_conditions(project.project_id, offset=0, limit=10, db=db)
        assert page["storage"] == "database" and page["total"] == 2
        assert {row["dose"] for row in page["items"]} == {"1 uM", "10 uM"}
        assert any(row["rank_within_condition"] == "1.5" for row in page["items"])
        stability = project_s2_stability(project.project_id, top_n=1, db=db)
        assert stability["storage"] == "database"
        assert len(stability["items"]) == 1
        assert stability["items"][0]["dose_count"] == 2
        assert stability["items"][0]["top_n_condition_count"] == 0
        assert "不合并WTCS" in stability["aggregation_policy"]
        other = create_project(ProjectCreate(slug=f"test-s2-other-{uuid4().hex[:8]}", name="S2 条件结果项目隔离", disease_id="MONDO:0000002"), db)
        assert project_s2_conditions(other.project_id, offset=0, limit=10, db=db)["available"] is False
        assert project_s2_stability(other.project_id, top_n=1, db=db)["available"] is False
        again = import_project_s2(project.project_id, S2ImportRequest(analysis_run_id=s2_run_id), db)
        assert again["idempotent"] is True and again["condition_count"] == 2
        assert candidates(project.project_id, offset=0, limit=50, db=db)["total"] == 0
    finally:
        db.rollback()
        shutil.rmtree(project_root, ignore_errors=True)


def test_project_s2_run_registers_cli_provenance_and_imports_condition_results(tmp_path, db, monkeypatch):
    import csv
    import hashlib
    import json
    import shutil
    from pathlib import Path
    from types import SimpleNamespace
    from uuid import uuid4

    import app.main as main_module

    project = create_project(ProjectCreate(slug=f"test-s2-run-{uuid4().hex[:8]}", name="S2 启动接口软件测试", disease_id="MONDO:0000003"), db)
    project.status = "test_only"
    db.commit()
    root = Path(__file__).resolve().parents[2]
    s1_token = uuid4().hex
    s1_run_id = f"ANALYSIS_RUN:S1_PRECOMPUTED:{s1_token}"
    s1_dir = root / "data" / "processed" / "projects" / project.project_id / "s1" / s1_token
    project_root = root / "data" / "processed" / "projects" / project.project_id
    s1_dir.mkdir(parents=True, exist_ok=False)
    s1_path = s1_dir / "limma_all_genes.csv"
    s1_path.write_text("gene_id,logFC,adj.P.Val,t\nG1,1,0.01,4\nG2,-1,0.01,-4\n", encoding="utf-8")
    s1_hash = hashlib.sha256(s1_path.read_bytes()).hexdigest()
    db.add(AnalysisRun(analysis_run_id=s1_run_id, project_id=project.project_id, run_type="s1_precomputed_deg", status="completed", parameters={"output_sha256":s1_hash}, software_versions={}, input_hashes={}))
    db.commit()

    def fake_subprocess_run(command, *, cwd, stdout, stderr, timeout, check):
        run_id = command[command.index("--analysis-run-id") + 1]
        assert "--test-only" in command and "--max-signatures" in command
        token = run_id.rsplit(":", 1)[-1]
        result_dir = project_root / "s2" / token
        result_dir.mkdir(parents=True, exist_ok=False)
        (result_dir / "signature_scores.csv").write_text("sig_id,pert_id,WTCS\nSIG-TEST,BRD-TEST,-0.3\n", encoding="utf-8")
        (result_dir / "s1_gene_mapping_audit.csv").write_text("gene_id,mapping_status\nG1,mapped\n", encoding="utf-8")
        (result_dir / "qc_summary.json").write_text("{}", encoding="utf-8")
        condition_path = result_dir / "drug_condition_summary.csv"
        with condition_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["pert_id", "drug_name", "cell_id", "pert_type", "dose", "time", "n_signatures", "WTCS_median", "rank_within_condition"])
            writer.writeheader()
            writer.writerow({"pert_id":"BRD-TEST", "drug_name":"Synthetic test compound", "cell_id":"MCF7", "pert_type":"trt_cp", "dose":"1 uM", "time":"24 h", "n_signatures":"1", "WTCS_median":"-0.3", "rank_within_condition":"1"})
        outputs = {}
        for name in ("signature_scores.csv", "s1_gene_mapping_audit.csv", "qc_summary.json", "drug_condition_summary.csv"):
            path = result_dir / name
            outputs[name] = {"bytes":path.stat().st_size, "sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
        (result_dir / "run_manifest.json").write_text(json.dumps({"analysis_run_id":run_id, "project_id":project.project_id, "s1_analysis_run_id":s1_run_id, "s1_input_sha256":s1_hash, "status":"completed", "test_only":True, "software_versions":{"pipeline":"synthetic route test"}, "inputs":{}, "outputs":outputs, "qc_summary":{"scored_signatures":1, "scored_drugs":1, "dose_time_condition_rows":1, "cell_id":"MCF7", "pert_type":"trt_cp", "time":"24 h", "dose_policy":"retain dose strata"}}, ensure_ascii=False), encoding="utf-8")
        stdout.write(b"synthetic subprocess used only to test orchestration\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(main_module.subprocess, "run", fake_subprocess_run)
    try:
        response = run_project_s2(project.project_id, S2RunRequest(s1_analysis_run_id=s1_run_id, identifier_type="symbol", max_signatures=2), db)
        assert response["status"] == "completed" and response["test_only"] is True
        assert response["condition_count"] == 1 and response["log_sha256"]
        page = project_s2_conditions(project.project_id, offset=0, limit=10, db=db)
        assert page["storage"] == "database" and page["items"][0]["pert_id"] == "BRD-TEST"
        run = db.get(AnalysisRun, response["analysis_run_id"])
        assert run.status == "completed" and run.parameters["test_only"] is True
    finally:
        db.rollback()
        shutil.rmtree(project_root, ignore_errors=True)

