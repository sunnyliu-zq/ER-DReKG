from contextlib import asynccontextmanager
import csv
import hashlib
import json
import os
import platform
from pathlib import Path, PurePosixPath
import subprocess
import sys
from statistics import median
from typing import Literal
from uuid import UUID, uuid4, uuid5

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from sqlalchemy import and_, func, insert, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.bootstrap import seed_historical_project
from app.core.config import settings
from app.db import SessionLocal, get_db
from app.evidence_matrix import evidence_matrix
from app.graph_repository import get_evidence, get_subgraph, graph_counts, mechanism_for_pert_id, search_nodes
from app.ingestion import inspect_expression_file, write_request_stream
from app.kg06 import build_evidence_package, kg06_coverage_rows
from app.kg07 import interpret as kg07_interpret, rule_template as kg07_rule_template
from app.models import AnalysisRun, Candidate, Dataset, DiseaseContext, EvidenceRecord, GraphBuild, Project, S2ConditionResult, Study
from app.schemas import DatasetCreate, DatasetOut, DatasetStudyLink, ExternalS1RankingItem, ExternalS1ValidationOut, GraphBundleRequest, JobStatusOut, KG06CoverageOut, KG06EvidencePackageOut, LimmaMicroarrayS1Request, PrecomputedS1Request, ProjectCreate, ProjectOut, RNASeqS1Request, RunComparisonOut, RunOut, S1EnrichmentRequest, S2ImportRequest, S2RunRequest, StudyCreate, StudyOut, SubgraphOut
from app.s1_genes import RESULTS_PATH, gene_items, volcano_points
from app.s1_expression import run_limma_microarray, run_rnaseq_edger_voom, run_project_s1_enrichment
from app.s1_pathways import COLLECTIONS, pathway_items, pathway_summary
from app.s1_precomputed import standardize_precomputed_deg
from app.s2_stability import summarize_condition_stability
from app.routers.collab import router as collab_router
from app.interpretation.evidence import EvidenceError, resolve_graph_build as resolve_kg_graph_build
from app.interpretation.router import router as interpretation_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    with SessionLocal() as db:
        seed_historical_project(db)
    yield


app = FastAPI(title="DiseaseGraph Lab API", version="0.1.0", lifespan=lifespan)
API = "/api/v1"
app.include_router(collab_router)
app.include_router(interpretation_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get(f"{API}/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return list(db.scalars(select(Project).order_by(Project.created_at.desc())))


@app.post(f"{API}/projects", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    if db.scalar(select(Project).where(Project.slug == payload.slug)):
        raise HTTPException(409, "project slug already exists")
    project = Project(slug=payload.slug, name=payload.name, disease_id=payload.disease_id, status=payload.status)
    db.add(project); db.commit(); db.refresh(project)
    return project


@app.get(f"{API}/projects/{{project_id}}", response_model=ProjectOut)
def get_project(project_id: str, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project: raise HTTPException(404, "project not found")
    return project


@app.get(f"{API}/projects/{{project_id}}/datasets")
def list_datasets(project_id: str, db: Session = Depends(get_db)):
    if not db.get(Project, project_id): raise HTTPException(404, "project not found")
    rows = list(db.scalars(select(Dataset).where(Dataset.project_id == project_id).order_by(Dataset.created_at.desc())))
    return {"project_id": project_id, "items": [{"dataset_id": row.dataset_id, "study_id": row.study_id, "study": ({"accession": row.study.accession, "title": row.study.title} if row.study else None), "source": row.source, "release": row.release, "sha256": row.sha256, "state": row.state, "license_status": row.license_status, "filename": row.metadata_json.get("original_filename"), "validation": {key: row.metadata_json.get(key) for key in ["dataset_type", "detected_format", "row_count", "column_count", "sample_count", "group_count", "sample_annotation_required"] if key in row.metadata_json}} for row in rows]}


@app.get(f"{API}/projects/{{project_id}}/studies", response_model=list[StudyOut])
def list_studies(project_id: str, db: Session = Depends(get_db)):
    if not db.get(Project, project_id): raise HTTPException(404, "project not found")
    return list(db.scalars(select(Study).where(Study.project_id == project_id).order_by(Study.created_at, Study.accession)))


@app.patch(f"{API}/projects/{{project_id}}/datasets/{{dataset_id}}/study", response_model=DatasetOut)
def link_dataset_to_study(project_id: str, dataset_id: str, payload: DatasetStudyLink, db: Session = Depends(get_db)):
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.project_id != project_id:
        raise HTTPException(404, "dataset not found in this project")
    study = db.get(Study, payload.study_id)
    if study is None or study.project_id != project_id:
        raise HTTPException(422, "study_id must refer to a study in this project")
    dataset.study_id = study.study_id
    db.commit()
    db.refresh(dataset)
    return dataset


@app.post(f"{API}/projects/{{project_id}}/studies", response_model=StudyOut, status_code=201)
def create_study(project_id: str, payload: StudyCreate, db: Session = Depends(get_db)):
    if db.get(Project, project_id) is None: raise HTTPException(404, "project not found")
    accession = payload.accession.strip().upper()
    title = payload.title.strip()
    organism = payload.organism.strip()
    existing = db.scalar(select(Study).where(Study.project_id == project_id, Study.accession == accession))
    if existing is not None:
        if (existing.title, existing.organism, existing.design_json) != (title, organism, payload.design_json):
            raise HTTPException(409, "study accession already exists in this project with different metadata")
        return existing
    study = Study(study_id=str(uuid5(UUID(project_id), f"study:{accession}")), project_id=project_id,
                  accession=accession, title=title, organism=organism, design_json=payload.design_json)
    db.add(study)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Study).where(Study.project_id == project_id, Study.accession == accession))
        if existing is None or (existing.title, existing.organism, existing.design_json) != (title, organism, payload.design_json):
            raise HTTPException(409, "study accession conflicts with an existing record")
        return existing
    db.refresh(study)
    return study


@app.get(f"{API}/projects/{{project_id}}/runs", response_model=list[RunOut])
def list_runs(project_id: str, db: Session = Depends(get_db)):
    if not db.get(Project, project_id): raise HTTPException(404, "project not found")
    return list(db.scalars(select(AnalysisRun).where(AnalysisRun.project_id == project_id).order_by(AnalysisRun.created_at.desc())))


@app.get(f"{API}/jobs/{{job_id}}", response_model=JobStatusOut)
def job_status(job_id: str, db: Session = Depends(get_db)):
    # Current S1/S2/KG work is synchronous; the AnalysisRun ID doubles as its
    # stable status handle until a separate async Job store is introduced.
    run = db.get(AnalysisRun, job_id)
    if run is None:
        raise HTTPException(404, "job/AnalysisRun not found")
    terminal = run.status in {"completed", "failed", "materialized", "materialized_snapshot"}
    return {
        "job_id": run.analysis_run_id,
        "analysis_run_id": run.analysis_run_id,
        "project_id": run.project_id,
        "state": run.status,
        "progress": 100 if run.status in {"completed", "materialized", "materialized_snapshot"} else 0 if run.status == "failed" else None,
        "created_at": run.created_at,
        "description": "同步 AnalysisRun 状态；任务队列未启用。" if terminal else "同步 AnalysisRun 尚未到达终态。",
    }


def _run_output_summary(run: AnalysisRun, db: Session) -> dict:
    params = run.parameters or {}
    if run.run_type in {"s1_precomputed_deg", "s1_limma_microarray", "s1_rnaseq_edger_voom"}:
        keys = ("dataset_id", "contrast", "unique_genes", "significant_genes", "up", "down", "fdr_threshold", "test_only")
        return {key: params[key] for key in keys if key in params}
    if run.run_type == "s1_pathway_enrichment":
        return {"s1_analysis_run_id": params.get("s1_analysis_run_id"), "significant_pathway_counts": params.get("significant_pathway_counts", {})}
    if run.run_type == "s2_lincs":
        qc = params.get("qc_summary", {})
        return {
            "s1_analysis_run_id": params.get("s1_analysis_run_id"),
            "condition_count": params.get("condition_count", 0),
            "signature_count": qc.get("scored_signatures"),
            "drug_count": qc.get("scored_drugs"),
            "cell_id": qc.get("cell_id"),
            "pert_type": qc.get("pert_type"),
            "time": qc.get("time"),
            "dose_policy": qc.get("dose_policy"),
            "test_only": params.get("test_only", False),
        }
    if run.run_type == "kg_graph_bundle":
        bundle_dir = params.get("bundle_dir")
        manifest_path = None
        if isinstance(bundle_dir, str):
            root = Path(__file__).resolve().parents[2]
            candidate = (root / bundle_dir / "bundle_manifest.json").resolve()
            if candidate.is_relative_to(root.resolve()) and candidate.is_file():
                manifest_path = candidate
        if manifest_path:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            return {"counts": manifest.get("counts", {}), "test_only": manifest.get("test_only"), "manifest_status": manifest.get("status")}
        return {"counts": params.get("counts", {}), "test_only": params.get("test_only")}
    if run.run_type == "kg_graph_build":
        build = db.scalar(select(GraphBuild).where(GraphBuild.analysis_run_id == run.analysis_run_id, GraphBuild.project_id == run.project_id))
        if build:
            return {"graph_build_id": build.graph_build_id, "graph_source": build.graph_source, "recorded_snapshot_summary": build.summary_json}
    return {"parameters": params}


def _s2_run_comparison(project_id: str, run_a: AnalysisRun, run_b: AnalysisRun, db: Session) -> dict:
    rows_a = list(db.scalars(select(S2ConditionResult).where(
        S2ConditionResult.project_id == project_id,
        S2ConditionResult.analysis_run_id == run_a.analysis_run_id,
    )))
    rows_b = list(db.scalars(select(S2ConditionResult).where(
        S2ConditionResult.project_id == project_id,
        S2ConditionResult.analysis_run_id == run_b.analysis_run_id,
    )))
    map_a = {(row.condition_key, row.pert_id): row for row in rows_a}
    map_b = {(row.condition_key, row.pert_id): row for row in rows_b}
    shared = map_a.keys() & map_b.keys()

    def number(value):
        try:
            result = float(value)
            return result if result == result and abs(result) != float("inf") else None
        except (TypeError, ValueError):
            return None

    wtcs_deltas = []
    rank_deltas = []
    for key in shared:
        a = map_a[key].condition_json
        b = map_b[key].condition_json
        a_wtcs = number(a.get("WTCS_median"))
        b_wtcs = number(b.get("WTCS_median"))
        a_rank = number(a.get("rank_within_condition"))
        b_rank = number(b.get("rank_within_condition"))
        if a_wtcs is not None and b_wtcs is not None:
            wtcs_deltas.append(b_wtcs - a_wtcs)
        if a_rank is not None and b_rank is not None:
            rank_deltas.append(b_rank - a_rank)
    return {
        "comparison_unit": "same exact condition_key and LINCS pert_id; dose/time strata are never pooled",
        "rows_run_a": len(map_a),
        "rows_run_b": len(map_b),
        "shared_condition_perturbagen_rows": len(shared),
        "only_in_run_a": len(map_a.keys() - map_b.keys()),
        "only_in_run_b": len(map_b.keys() - map_a.keys()),
        "wtcs_delta_run_b_minus_run_a": {
            "n": len(wtcs_deltas),
            "mean": sum(wtcs_deltas) / len(wtcs_deltas) if wtcs_deltas else None,
            "median": median(wtcs_deltas) if wtcs_deltas else None,
            "median_absolute": median([abs(value) for value in wtcs_deltas]) if wtcs_deltas else None,
        },
        "within_condition_rank_delta_run_b_minus_run_a": {
            "n": len(rank_deltas),
            "median": median(rank_deltas) if rank_deltas else None,
            "median_absolute": median([abs(value) for value in rank_deltas]) if rank_deltas else None,
        },
    }


@app.get(f"{API}/projects/{{project_id}}/runs/compare", response_model=RunComparisonOut)
def compare_project_runs(project_id: str, run_a_id: str = Query(..., min_length=10, max_length=100), run_b_id: str = Query(..., min_length=10, max_length=100), db: Session = Depends(get_db)):
    if run_a_id == run_b_id:
        raise HTTPException(422, "请选择两个不同的 AnalysisRun")
    run_a = db.get(AnalysisRun, run_a_id)
    run_b = db.get(AnalysisRun, run_b_id)
    if not run_a or not run_b or run_a.project_id != project_id or run_b.project_id != project_id:
        raise HTTPException(404, "AnalysisRun 不存在或不属于当前项目")
    if run_a.status != "completed" or run_b.status != "completed":
        raise HTTPException(409, "只有已完成的 AnalysisRun 可以比较")
    if run_a.run_type != run_b.run_type:
        raise HTTPException(422, "必须选择同一分析类型的两个 AnalysisRun")

    hashes_a = run_a.input_hashes or {}
    hashes_b = run_b.input_hashes or {}
    all_hash_keys = hashes_a.keys() | hashes_b.keys()
    provenance = {
        "identical_input_hashes": hashes_a == hashes_b,
        "shared_hash_keys": sum(1 for key in hashes_a.keys() & hashes_b.keys() if hashes_a[key] == hashes_b[key]),
        "changed_or_missing_hash_keys": sorted(key for key in all_hash_keys if hashes_a.get(key) != hashes_b.get(key)),
        "different_software_versions": {key: {"run_a": (run_a.software_versions or {}).get(key), "run_b": (run_b.software_versions or {}).get(key)} for key in (run_a.software_versions or {}).keys() | (run_b.software_versions or {}).keys() if (run_a.software_versions or {}).get(key) != (run_b.software_versions or {}).get(key)},
        "different_parameter_keys": sorted(key for key in (run_a.parameters or {}).keys() | (run_b.parameters or {}).keys() if (run_a.parameters or {}).get(key) != (run_b.parameters or {}).get(key)),
    }
    summary_a = _run_output_summary(run_a, db)
    summary_b = _run_output_summary(run_b, db)
    output_comparison = {"run_a": summary_a, "run_b": summary_b}
    if run_a.run_type in {"s1_precomputed_deg", "s1_limma_microarray", "s1_rnaseq_edger_voom"}:
        metric_keys = {"unique_genes", "significant_genes", "up", "down"}
        output_comparison["numeric_deltas_run_b_minus_run_a"] = {key: summary_b[key] - summary_a[key] for key in metric_keys if isinstance(summary_a.get(key), (int, float)) and isinstance(summary_b.get(key), (int, float))}
    elif run_a.run_type == "s2_lincs":
        output_comparison["condition_matched_comparison"] = _s2_run_comparison(project_id, run_a, run_b, db)
    elif run_a.run_type == "kg_graph_build":
        output_comparison["interpretation"] = "图节点/边数量来自各自登记时快照；若需当前实时计数，应对指定 GraphBuild 分别请求图摘要接口。"
    return {
        "project_id": project_id,
        "run_type": run_a.run_type,
        "run_a": {"analysis_run_id": run_a.analysis_run_id, "run_type": run_a.run_type, "status": run_a.status, "created_at": run_a.created_at, "parameters": run_a.parameters or {}, "software_versions": run_a.software_versions or {}, "input_hashes": hashes_a, "output_summary": summary_a},
        "run_b": {"analysis_run_id": run_b.analysis_run_id, "run_type": run_b.run_type, "status": run_b.status, "created_at": run_b.created_at, "parameters": run_b.parameters or {}, "software_versions": run_b.software_versions or {}, "input_hashes": hashes_b, "output_summary": summary_b},
        "provenance_comparison": provenance,
        "output_comparison": output_comparison,
    }


def _latest_precomputed_s1(project_id: str, db: Session):
    return db.scalar(
        select(AnalysisRun)
        .where(AnalysisRun.project_id == project_id, AnalysisRun.run_type.in_(["s1_precomputed_deg", "s1_limma_microarray", "s1_rnaseq_edger_voom"]), AnalysisRun.status == "completed")
        .order_by(AnalysisRun.created_at.desc(), AnalysisRun.analysis_run_id.desc())
    )


def _latest_s1_enrichment(project_id: str, db: Session):
    return db.scalar(select(AnalysisRun).where(AnalysisRun.project_id == project_id, AnalysisRun.run_type == "s1_pathway_enrichment", AnalysisRun.status == "completed").order_by(AnalysisRun.created_at.desc(), AnalysisRun.analysis_run_id.desc()))


def _precomputed_s1_path(project_id: str, run: AnalysisRun) -> Path:
    root = Path(__file__).resolve().parents[2]
    run_directory = run.analysis_run_id.rsplit(":", 1)[-1]
    expected = (root / "data" / "processed" / "projects" / project_id / "s1" / run_directory / "limma_all_genes.csv").resolve()
    if not expected.is_relative_to((root / "data" / "processed" / "projects" / project_id).resolve()):
        raise HTTPException(500, "S1 结果路径不在当前项目目录内")
    return expected


def _uploaded_dataset_source(dataset: Dataset, project_id: str, repo_root: Path) -> Path:
    relative = dataset.metadata_json.get("storage_path")
    if not isinstance(relative, str):
        raise HTTPException(409, "数据集缺少受控存储路径")
    configured_root = Path(settings.data_upload_dir)
    if not configured_root.is_absolute():
        configured_root = repo_root / configured_root
    allowed_root = (configured_root / project_id / dataset.dataset_id).resolve()
    stored = Path(relative)
    source = (stored if stored.is_absolute() else repo_root / stored).resolve()
    if not source.is_relative_to(allowed_root) or not source.is_file():
        raise HTTPException(409, "数据集文件不在本项目受控上传目录中或已丢失")
    if len(dataset.sha256) != 64 or _hash_file(source) != dataset.sha256:
        raise HTTPException(409, "数据集文件 SHA-256 与登记值不一致")
    return source


def _s2_result_path(project_id: str, analysis_run_id: str) -> Path:
    token = analysis_run_id.rsplit(":", 1)[-1]
    try:
        UUID(token)
    except ValueError as error:
        raise HTTPException(422, "S2 AnalysisRun ID 格式无效") from error
    root = Path(__file__).resolve().parents[2]
    project_root = (root / "data" / "processed" / "projects" / project_id).resolve()
    result_dir = (project_root / "s2" / token).resolve()
    if not result_dir.is_relative_to(project_root):
        raise HTTPException(422, "S2 结果路径越界")
    return result_dir


def _parse_rank_field(value: str | None) -> float:
    try:
        rank = float(value) if value is not None else float("nan")
    except (TypeError, ValueError):
        rank = float("nan")
    if not (rank > 0 and rank < float("inf")):
        raise HTTPException(422, "S2 条件排名必须是正的有限数值")
    return rank


def _s1_source(project: Project, db: Session):
    if project.slug == "erpos-gse42568":
        return RESULTS_PATH, 0.05, None
    run = _latest_precomputed_s1(project.project_id, db)
    if run is None:
        return None, 0.05, None
    path = _precomputed_s1_path(project.project_id, run)
    if not path.is_file():
        raise HTTPException(503, "登记的项目 S1 结果文件缺失")
    return path, float(run.parameters.get("fdr_threshold", 0.05)), run


@app.get(f"{API}/projects/{{project_id}}/s1/summary")
def s1_summary(project_id: str, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project: raise HTTPException(404, "project not found")
    if project.slug != "erpos-gse42568":
        run = _latest_precomputed_s1(project_id, db)
        if run:
            params = run.parameters
            threshold = float(params.get("fdr_threshold", 0.05))
            significant = int(params.get("significant_genes", 0))
            source = {"s1_limma_microarray": "limma normalized log2 microarray", "s1_rnaseq_edger_voom": "edgeR filterByExpr + TMM + limma-voom raw integer counts", "s1_precomputed_deg": "project-scoped precomputed DEG; not recalculated"}.get(run.run_type, "project-scoped S1 result")
            enrichment = _latest_s1_enrichment(project_id, db)
            if enrichment and enrichment.parameters.get("s1_analysis_run_id") != run.analysis_run_id:
                enrichment = None
            enrichment_counts = enrichment.parameters.get("significant_pathway_counts", {}) if enrichment else {}
            return {"project_id": project_id, "available": True, "dataset": params.get("dataset_id"), "contrast": params.get("contrast"), "tested_genes": params.get("unique_genes", 0), "significant_genes": significant, "significant_genes_fdr05": significant if threshold == 0.05 else None, "up": params.get("up", 0), "down": params.get("down", 0), "pathway_enrichment_available": enrichment is not None, "pathway_counts": enrichment_counts, "pathway_analysis_run_id": enrichment.analysis_run_id if enrichment else None, "source": source, "analysis_run_id": run.analysis_run_id, "fdr_threshold": threshold, "gene_identifier_column": params.get("gene_identifier_column"), "duplicate_gene_rows_removed": params.get("duplicate_rows_removed", 0), "test_only": bool(params.get("test_only", False))}
        return {"project_id": project_id, "available": False, "dataset": None, "contrast": None, "tested_genes": 0, "significant_genes_fdr05": 0, "up": 0, "down": 0, "pathway_enrichment_available": False, "source": None}
    pathways = pathway_summary()
    return {"project_id": project_id, "available": True, "dataset": "GSE42568", "contrast": "Tumor - Normal", "tested_genes": 21655, "significant_genes_fdr05": 7066, "up": 3422, "down": 3644, "pathway_enrichment_available": pathways["available"], "pathway_counts": pathways["counts"], "pathway_gene_universe": pathways.get("mapped_universe"), "pathway_input_sha256": pathways.get("input_sha256"), "source": "existing limma and pathway enrichment outputs; not recalculated"}


@app.get(f"{API}/projects/{{project_id}}/s1/external-validation", response_model=ExternalS1ValidationOut)
def s1_external_validation(project_id: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")
    if project.slug != "erpos-gse42568":
        return ExternalS1ValidationOut(project_id=project_id, available=False, reason="该项目尚无独立外部队列验证结果", limit=limit, offset=offset)

    result_dir = Path(__file__).resolve().parents[2] / "experiments" / "exp010_external_s1" / "results"
    manifest_path = result_dir / "run_manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(503, "Exp010 外部队列验证 manifest 缺失")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "PASS":
        raise HTTPException(503, "Exp010 外部队列验证未通过验收")

    def verified_output(filename: str) -> Path:
        path = result_dir / filename
        expected_path = f"experiments/exp010_external_s1/results/{filename}"
        hashes = manifest.get("outputs_sha256_all", {})
        expected_hash = next((value for key, value in hashes.items() if key.replace("\\", "/") == expected_path), None)
        if not path.is_file() or not expected_hash or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise HTTPException(503, f"Exp010 输出文件缺失或 SHA-256 不匹配：{filename}")
        return path

    summary_path = verified_output("cross_cohort_summary.json")
    ranking_path = verified_output("external_s1_drug_ranking.csv")
    inventory_path = verified_output("external_dataset_inventory.csv")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    with inventory_path.open(encoding="utf-8-sig", newline="") as handle:
        inventory = next((row for row in csv.DictReader(handle) if row.get("dataset") == "GSE139038"), None)
    if inventory is None:
        raise HTTPException(503, "Exp010 manifest 中缺少 GSE139038 队列设计记录")

    items: list[ExternalS1RankingItem] = []
    with ranking_path.open(encoding="utf-8-sig", newline="") as handle:
        for row_index, row in enumerate(csv.DictReader(handle)):
            if row_index < offset:
                continue
            if len(items) >= limit:
                break
            def number(key: str) -> float | None:
                value = row.get(key, "")
                return float(value) if value not in {None, "", "NA", "NaN"} else None

            items.append(ExternalS1RankingItem(
                pert_id=row["pert_id"], drug_name=row["drug_name"], cell_id=row["cell_id"], pert_type=row["pert_type"],
                dose=row["dose"], time=row["time"], n_signatures=int(row["n_signatures"]),
                WTCS_median=number("WTCS_median"), CC_median=number("CC_median"), TAS_median=number("TAS_median"),
                external_rank=number("external_rank"), B200_WTCS_median=number("B200_WTCS_median"), B200_rank=number("B200_rank"),
            ))
    return ExternalS1ValidationOut(
        project_id=project_id, available=True, source_dataset="GSE139038", validation_run="Exp010",
        sample_design={"platform": inventory["platform"], "total_samples": int(inventory["n_samples"]), "ER_positive_tumors": int(inventory["ER_positive_n"]), "normal_tissue_samples": int(inventory["normal_n"]), "paired_ER_positive_patients": int(manifest["external_paired_ERpositive_patient_n"]), "discovery_sample_overlap": 0},
        summary=summary, offset=offset, limit=limit, total_conditions=int(summary["external_drug_conditions_n"]), items=items,
        provenance={"manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(), "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(), "ranking_sha256": hashlib.sha256(ranking_path.read_bytes()).hexdigest(), "selection": "GSE139038 ER-positive patient-matched Tumor − Normal; LINCS Q2; BING-restricted external S1; WTCS rankings remain stratified by dose/time", "interpretation": "External molecular replication and S2 rank comparison only; not clinical efficacy evidence"},
    )


@app.get(f"{API}/projects/{{project_id}}/s1/genes")
def s1_genes(project_id: str, direction: Literal["all", "up", "down"] = "all", limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), significant_only: bool = True, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project: raise HTTPException(404, "project not found")
    path, fdr_threshold, run = _s1_source(project, db)
    if path is None:
        return {"project_id": project_id, "available": False, "direction": direction, "total": 0, "offset": offset, "limit": limit, "items": []}
    if not path.is_file():
        raise HTTPException(503, "historical S1 limma result is unavailable")
    result = gene_items(direction, limit, offset, significant_only, path=path, fdr_threshold=fdr_threshold)
    return {"project_id": project_id, "available": True, "analysis_run_id": run.analysis_run_id if run else "PHASEB_OPTIMUSKG32_20261113", **result}


@app.get(f"{API}/projects/{{project_id}}/s1/volcano")
def s1_volcano(project_id: str, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project: raise HTTPException(404, "project not found")
    path, fdr_threshold, run = _s1_source(project, db)
    if path is None:
        return {"project_id": project_id, "available": False, "total": 0, "points": []}
    if not path.is_file():
        raise HTTPException(503, "historical S1 limma result is unavailable")
    return {"project_id": project_id, "available": True, "analysis_run_id": run.analysis_run_id if run else "PHASEB_OPTIMUSKG32_20261113", **volcano_points(path, fdr_threshold)}


@app.get(f"{API}/projects/{{project_id}}/s1/pathways")
def s1_pathways(project_id: str, collection: str = Query(...), limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), significant_only: bool = True, analysis_run_id: str | None = None, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project: raise HTTPException(404, "project not found")
    if collection not in COLLECTIONS:
        raise HTTPException(422, "unsupported S1 pathway collection")
    enrichment = db.get(AnalysisRun, analysis_run_id) if analysis_run_id else _latest_s1_enrichment(project_id, db)
    if enrichment and analysis_run_id is None:
        latest_s1 = _latest_precomputed_s1(project_id, db)
        if latest_s1 and enrichment.parameters.get("s1_analysis_run_id") != latest_s1.analysis_run_id:
            enrichment = None
    if enrichment:
        if enrichment.project_id != project_id or enrichment.run_type != "s1_pathway_enrichment" or enrichment.status != "completed":
            raise HTTPException(404, "S1 pathway enrichment run not found in this project")
        run_token = enrichment.analysis_run_id.rsplit(":", 1)[-1]
        result_dir = (Path(__file__).resolve().parents[2] / "data" / "processed" / "projects" / project_id / "s1_enrichment" / run_token).resolve()
        project_root = (Path(__file__).resolve().parents[2] / "data" / "processed" / "projects" / project_id).resolve()
        if not result_dir.is_relative_to(project_root):
            raise HTTPException(500, "富集结果路径越界")
        return {"project_id": project_id, "analysis_run_id": enrichment.analysis_run_id, "available": True, **pathway_items(collection, limit, offset, significant_only, result_dir=result_dir)}
    if analysis_run_id:
        raise HTTPException(404, "S1 pathway enrichment run not found in this project")
    if project.slug == "erpos-gse42568":
        return {"project_id": project_id, "available": True, **pathway_items(collection, limit, offset, significant_only)}
    return {"project_id": project_id, "available": False, "collection": collection, "total": 0, "offset": offset, "limit": limit, "items": []}


@app.get(f"{API}/projects/{{project_id}}/s2/candidates")
def candidates(project_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    if not db.get(Project, project_id): raise HTTPException(404, "project not found")
    query = select(Candidate).where(Candidate.project_id == project_id).order_by(Candidate.rank).offset(offset).limit(limit)
    rows = list(db.scalars(query))
    total = db.scalar(select(func.count()).select_from(Candidate).where(Candidate.project_id == project_id)) or 0
    return {"project_id": project_id, "items": [{"candidate_id": r.candidate_id, "pert_id": r.pert_id, "drug_name": r.drug_name, "rank": r.rank, "score_name": r.score_name, "score_value": r.score_value, "evidence": r.evidence_json} for r in rows], "offset": offset, "limit": limit, "total": total, "truncated": offset + len(rows) < total}


@app.get(f"{API}/projects/{{project_id}}/kg06/coverage", response_model=KG06CoverageOut)
def kg06_coverage(project_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=50), graph_build_id: str | None = None, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    build = db.get(GraphBuild, graph_build_id) if graph_build_id else db.scalar(
        select(GraphBuild).where(GraphBuild.project_id == project_id, GraphBuild.status == "materialized")
        .order_by(GraphBuild.created_at.desc(), GraphBuild.graph_build_id.desc()).limit(1)
    )
    if build is None or build.project_id != project_id or build.status != "materialized":
        raise HTTPException(404, "materialized GraphBuild not found in this project")
    root = Path(__file__).resolve().parents[2]
    audit_dir = root / "experiments" / "kg06_mechanism_evidence" / "results"
    manifest_path = audit_dir / "kg06_audit_manifest.json"
    coverage_path = audit_dir / "top50_mechanism_coverage.csv"
    if not manifest_path.is_file() or not coverage_path.is_file():
        raise HTTPException(503, "KG06 Top50 coverage audit has not been generated")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "PASS" or manifest.get("schema", {}).get("graph_build_id") != build.graph_build_id:
        raise HTTPException(409, "KG06 coverage audit does not match the selected GraphBuild")
    expected_hash = manifest.get("outputs", {}).get("top50_mechanism_coverage.csv")
    if not expected_hash or _hash_file(coverage_path) != expected_hash:
        raise HTTPException(409, "KG06 coverage output hash does not match its audit manifest")
    items = kg06_coverage_rows(coverage_path)
    return {
        "project_id": project_id,
        "graph_build_id": build.graph_build_id,
        "audit_status": manifest["status"],
        "coverage": manifest["coverage"],
        "items": items[offset:offset + limit],
        "input_hashes": manifest["inputs"],
    }


def _resolve_kg_graph_build_or_http(db: Session, project_id: str, requested_id: str | None):
    """Resolve exactly one formal Neo4j GraphBuild for KG06/KG07."""
    try:
        return resolve_kg_graph_build(db, project_id, requested_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except EvidenceError as error:
        message = str(error)
        if "图谱源" in message:
            raise HTTPException(503, message) from error
        if "没有已完成" in message:
            raise HTTPException(404, message) from error
        raise HTTPException(409, message) from error


def _kg06_evidence_package_for_build(
    db: Session,
    project: Project,
    build: GraphBuild,
    pert_id: str,
):
    """Validate the selected build's bundle, then construct one package."""
    bundle_run = db.get(AnalysisRun, build.analysis_run_id)
    relative_bundle_dir = bundle_run.parameters.get("bundle_dir") if bundle_run is not None else None
    if not isinstance(relative_bundle_dir, str):
        raise HTTPException(409, "GraphBuild is missing its GraphBundle path")
    root = Path(__file__).resolve().parents[2]
    bundle_dir = (root / relative_bundle_dir).resolve()
    project_root = (root / "data" / "processed" / "projects" / project.project_id).resolve()
    if not bundle_dir.is_relative_to(project_root):
        raise HTTPException(422, "GraphBundle path is outside the selected project")
    for filename, expected in build.input_hashes.items():
        source_file = (bundle_dir / filename).resolve()
        if source_file.is_relative_to(bundle_dir) and source_file.is_file() and _hash_file(source_file) != expected:
            raise HTTPException(409, f"GraphBuild input hash mismatch: {filename}")
    try:
        return build_evidence_package(
            db,
            project,
            build,
            bundle_dir,
            pert_id,
            expected_graph_build_id=build.graph_build_id,
        )
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.get(f"{API}/projects/{{project_id}}/kg06/evidence-package/{{pert_id}}", response_model=KG06EvidencePackageOut)
def kg06_evidence_package(project_id: str, pert_id: str, graph_build_id: str | None = None, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    build = _resolve_kg_graph_build_or_http(db, project_id, graph_build_id)
    return _kg06_evidence_package_for_build(db, project, build, pert_id)


@app.post(f"{API}/projects/{{project_id}}/kg07/interpret/{{pert_id}}")
def kg07_interpretation(project_id: str, pert_id: str, mode: str = Query("kg_llm", pattern="^(kg_llm|direct_llm|kg_rule)$"), graph_build_id: str | None = None, db: Session = Depends(get_db)):
    """Generate an evidence-grounded interpretation for this project/build."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    build = _resolve_kg_graph_build_or_http(db, project_id, graph_build_id)
    package = _kg06_evidence_package_for_build(db, project, build, pert_id)
    if mode == "kg_rule":
        return kg07_rule_template(package, expected_graph_build_id=build.graph_build_id)
    try:
        return kg07_interpret(package, mode=mode, expected_graph_build_id=build.graph_build_id)
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error
    except (ValueError, KeyError, json.JSONDecodeError) as error:
        raise HTTPException(502, f"KG07 response validation failed: {type(error).__name__}") from error


@app.get(f"{API}/projects/{{project_id}}/s2/evidence-matrix")
def s2_evidence_matrix(project_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project: raise HTTPException(404, "project not found")
    if project.slug != "erpos-gse42568":
        return {"project_id": project_id, "available": False, "total": 0, "offset": offset, "limit": limit, "items": []}
    return {"project_id": project_id, **evidence_matrix(limit, offset)}


@app.get(f"{API}/projects/{{project_id}}/s2/candidate-evidence")
def s2_candidate_evidence(project_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")
    if project.slug != "erpos-gse42568":
        return {"project_id": project_id, "available": False, "total": 0, "offset": offset, "limit": limit, "items": []}

    repo_root = Path(__file__).resolve().parents[2]
    manifest_path = repo_root / "experiments" / "phase_f_integrated_evidence" / "phase_f_manifest.json"
    result_path = repo_root / "experiments" / "phase_f_integrated_evidence" / "candidate_evidence_with_kg.csv"
    if not manifest_path.is_file() or not result_path.is_file():
        raise HTTPException(503, "Phase F 候选证据矩阵尚未生成")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "PASS" or manifest.get("project_id") != project_id:
        raise HTTPException(409, "Phase F 清单项目或验收状态不匹配")
    expected_hash = manifest.get("output_sha256", {}).get(result_path.relative_to(repo_root).as_posix())
    if not expected_hash or _hash_file(result_path) != expected_hash:
        raise HTTPException(409, "Phase F 候选证据矩阵 SHA-256 不匹配")
    with result_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    expected_count = manifest.get("counts", {}).get("integrated_candidates")
    if expected_count != len(rows) or len({row.get("pert_id") for row in rows}) != len(rows):
        raise HTTPException(409, "Phase F 候选数或 perturbagen 唯一性与清单不匹配")
    return {
        "project_id": project_id,
        "available": True,
        "graph_build_id": manifest.get("graph_build_id"),
        "source_sha256": expected_hash,
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "items": rows[offset:offset + limit],
        "truncated": offset + limit < len(rows),
    }


def _latest_project_s2(project_id: str, db: Session):
    return db.scalar(select(AnalysisRun).where(AnalysisRun.project_id == project_id, AnalysisRun.run_type == "s2_lincs", AnalysisRun.status == "completed").order_by(AnalysisRun.created_at.desc(), AnalysisRun.analysis_run_id.desc()))


@app.post(f"{API}/projects/{{project_id}}/s2/import")
def import_project_s2(project_id: str, payload: S2ImportRequest, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    existing = db.get(AnalysisRun, payload.analysis_run_id)
    if existing:
        if existing.project_id != project_id or existing.run_type != "s2_lincs":
            raise HTTPException(409, "AnalysisRun ID 已属于其他项目或分析类型")
        persisted = db.scalar(select(func.count()).select_from(S2ConditionResult).where(S2ConditionResult.analysis_run_id == existing.analysis_run_id)) or 0
        if persisted:
            return {"project_id": project_id, "analysis_run_id": existing.analysis_run_id, "status": existing.status, "idempotent": True, "parameters": existing.parameters, "condition_count": persisted}
        # Backfill older imported runs from their already hash-verified result file.
        legacy_backfill = True
    else:
        legacy_backfill = False
    result_dir = _s2_result_path(project_id, payload.analysis_run_id)
    manifest_path = result_dir / "run_manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(404, "未找到该项目的 S2 运行清单")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(422, "S2 运行清单无法解析") from error
    if manifest.get("status") != "completed" or manifest.get("analysis_run_id") != payload.analysis_run_id or manifest.get("project_id") != project_id:
        raise HTTPException(409, "S2 结果未完成或项目/运行 ID 不匹配")
    if manifest.get("test_only") and project.status != "test_only":
        raise HTTPException(409, "test_only S2 结果不能导入科研项目")
    s1_run_id = manifest.get("s1_analysis_run_id")
    s1_run = db.get(AnalysisRun, s1_run_id) if isinstance(s1_run_id, str) else None
    if s1_run is None or s1_run.project_id != project_id or s1_run.run_type not in {"s1_precomputed_deg", "s1_limma_microarray", "s1_rnaseq_edger_voom"} or s1_run.status != "completed":
        raise HTTPException(409, "S2 清单未关联本项目已完成的 S1 AnalysisRun")
    s1_path = _precomputed_s1_path(project_id, s1_run)
    if not s1_path.is_file():
        raise HTTPException(409, "S2 所用 S1 结果缺失")
    actual_s1_hash = _hash_file(s1_path)
    recorded_s1_hash = manifest.get("s1_input_sha256")
    historical_source_hash = manifest.get("source_s1_input_sha256")
    historical_source_verified = False
    if recorded_s1_hash != actual_s1_hash:
        # A historical S2 result can be linked to a platform-standardized S1
        # run only when it records the exact original S1 upload hash and that
        # upload is still present, project-scoped, and independently verified.
        if manifest.get("provenance_kind") != "historical_output_import" or not isinstance(historical_source_hash, str):
            raise HTTPException(409, "S2 所用 S1 结果缺失或哈希与运行清单不一致")
        dataset_id = s1_run.parameters.get("dataset_id")
        source_dataset = db.get(Dataset, dataset_id) if isinstance(dataset_id, str) else None
        if (
            source_dataset is None
            or source_dataset.project_id != project_id
            or historical_source_hash != s1_run.parameters.get("input_sha256")
            or source_dataset.sha256 != historical_source_hash
        ):
            raise HTTPException(409, "历史 S2 输入不能追溯到所关联项目 S1 的原始数据集")
        try:
            _uploaded_dataset_source(source_dataset, project_id, Path(__file__).resolve().parents[2])
        except HTTPException as error:
            raise HTTPException(409, "历史 S2 对应的 S1 原始数据集哈希校验失败") from error
        historical_source_verified = True
    required_outputs = {"signature_scores.csv", "drug_condition_summary.csv", "s1_gene_mapping_audit.csv", "qc_summary.json"}
    outputs = manifest.get("outputs", {})
    if not required_outputs.issubset(outputs):
        raise HTTPException(422, "S2 输出清单缺少必需结果")
    for name in required_outputs:
        output_path = (result_dir / name).resolve()
        if not output_path.is_relative_to(result_dir) or not output_path.is_file() or _hash_file(output_path) != outputs[name].get("sha256"):
            raise HTTPException(409, f"S2 输出文件缺失或哈希不匹配：{name}")
    with (result_dir / "drug_condition_summary.csv").open(encoding="utf-8-sig", newline="") as handle:
        condition_count = sum(1 for _ in csv.DictReader(handle))
    if condition_count < 1:
        raise HTTPException(409, "S2 条件汇总没有可导入记录")
    try:
        if not legacy_backfill:
            run = AnalysisRun(
                analysis_run_id=payload.analysis_run_id,
                project_id=project_id,
                run_type="s2_lincs",
                status="completed",
                parameters={"result_directory": result_dir.relative_to(Path(__file__).resolve().parents[2]).as_posix(), "s1_analysis_run_id": s1_run_id, "condition_count": condition_count, "test_only": bool(manifest.get("test_only")), "qc_summary": manifest.get("qc_summary", {}), "condition_summary_sha256": outputs["drug_condition_summary.csv"].get("sha256"), "provenance_kind": manifest.get("provenance_kind", "project_analysis"), "historical_source_verified": historical_source_verified, "source_s1_input_sha256": historical_source_hash if historical_source_verified else None},
                software_versions=manifest.get("software_versions", {}),
                input_hashes={path: info.get("sha256", "") for path, info in manifest.get("inputs", {}).items()},
            )
            db.add(run)
            db.flush()
        else:
            run = existing
        with (result_dir / "drug_condition_summary.csv").open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                pert_id = row.get("pert_id", "").strip()
                condition_parts = [row.get(column, "") for column in ("cell_id", "pert_type", "time", "dose")]
                if not pert_id or any(not value for value in condition_parts):
                    raise HTTPException(422, "S2 条件行缺少 pert_id、cell/type/time/dose 字段")
                condition_key = json.dumps(condition_parts, ensure_ascii=False, separators=(",", ":"))
                result_id = f"S2_CONDITION:{payload.analysis_run_id.rsplit(':', 1)[-1]}:{hashlib.sha256((condition_key + pert_id).encode()).hexdigest()[:32]}"
                rank = _parse_rank_field(row.get("rank_within_condition"))
                db.add(S2ConditionResult(result_id=result_id, project_id=project_id, analysis_run_id=payload.analysis_run_id, pert_id=pert_id, condition_key=condition_key, rank_within_condition=rank, condition_json=row))
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, "S2 条件结果与已有运行版本冲突") from error
    return {"project_id": project_id, "analysis_run_id": run.analysis_run_id, "status": run.status, "idempotent": False, "backfilled": legacy_backfill, "condition_count": condition_count, "test_only": bool(manifest.get("test_only")), "persisted_condition_count": condition_count}


@app.post(f"{API}/projects/{{project_id}}/s2/run")
def run_project_s2(project_id: str, payload: S2RunRequest, db: Session = Depends(get_db)):
    """Run the project-scoped LINCS CLI and import its dose/time-stratified results."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        UUID(project_id)
    except ValueError as error:
        raise HTTPException(422, "project_id 必须为 UUID") from error
    s1_run = db.scalar(select(AnalysisRun).where(AnalysisRun.analysis_run_id == payload.s1_analysis_run_id, AnalysisRun.project_id == project_id))
    if s1_run is None or s1_run.run_type not in {"s1_precomputed_deg", "s1_limma_microarray", "s1_rnaseq_edger_voom"} or s1_run.status != "completed":
        raise HTTPException(409, "S2 必须引用本项目已完成的 S1 AnalysisRun")
    if payload.max_signatures is not None and project.status != "test_only":
        raise HTTPException(422, "限制 Signature 数的 S2 只能在 test_only 软件测试项目中运行")
    if project.status == "test_only" and payload.max_signatures is None:
        raise HTTPException(422, "test_only 项目必须设置 max_signatures，防止误启动全量筛选")
    root = Path(__file__).resolve().parents[2]
    s1_path = _precomputed_s1_path(project_id, s1_run)
    if not s1_path.is_file():
        raise HTTPException(409, "指定的 S1 输出文件缺失")
    s1_hash = _hash_file(s1_path)
    expected_hash = s1_run.parameters.get("output_sha256")
    if expected_hash and expected_hash != s1_hash:
        raise HTTPException(409, "S1 AnalysisRun 输出哈希与当前文件不一致")

    run_uuid = uuid4()
    token = str(run_uuid)
    run_id = f"ANALYSIS_RUN:S2_LINCS:{token}"
    log_dir = root / "data" / "processed" / "projects" / project_id / "s2_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{token}.log"
    params = payload.model_dump(mode="json")
    run = AnalysisRun(
        analysis_run_id=run_id,
        project_id=project_id,
        run_type="s2_lincs",
        status="running",
        parameters={"s1_analysis_run_id": payload.s1_analysis_run_id, "s1_input_sha256": s1_hash, "test_only": project.status == "test_only", "requested_parameters": params, "log_path": log_path.relative_to(root).as_posix()},
        software_versions={"pipeline": "project_screen.py"},
        input_hashes={s1_run.analysis_run_id: s1_hash, "s2_lincs/project_screen.py": _hash_file(root / "s2_lincs" / "project_screen.py")},
    )
    db.add(run)
    db.commit()

    s2_env_python = root / "s2_lincs" / ".venv" / "Scripts" / "python.exe"
    if not s2_env_python.is_file():
        s2_env_python = root / "s2_lincs" / ".venv" / "bin" / "python"
    script = root / "s2_lincs" / "project_screen.py"
    command = [
        str(s2_env_python), str(script), "--s1-table", str(s1_path),
        "--identifier-type", payload.identifier_type, "--project-id", project_id,
        "--s1-analysis-run-id", payload.s1_analysis_run_id, "--analysis-run-id", run_id,
        "--fdr-threshold", str(payload.fdr_threshold), "--top-n", str(payload.top_n),
        "--cell-id", payload.cell_id, "--perturbation-type", payload.perturbation_type,
        "--time", payload.time, "--min-distil-cc-q75", str(payload.min_distil_cc_q75),
    ]
    if payload.max_signatures is not None:
        command.extend(["--max-signatures", str(payload.max_signatures)])
    if project.status == "test_only":
        command.append("--test-only")
    try:
        if not s2_env_python.is_file() or not script.is_file():
            raise FileNotFoundError("S2 隔离 Python 环境或 project_screen.py 不存在")
        with log_path.open("wb") as log_handle:
            completed = subprocess.run(command, cwd=root, stdout=log_handle, stderr=subprocess.STDOUT, timeout=7200, check=False)
    except subprocess.TimeoutExpired as error:
        run.status = "failed"
        run.parameters = {**run.parameters, "error": "S2 CLI 超过 2 小时运行时限", "log_sha256": _hash_file(log_path) if log_path.exists() else None}
        db.commit()
        raise HTTPException(504, "S2 CLI 超过 2 小时运行时限；运行日志已保留") from error
    except OSError as error:
        run.status = "failed"
        run.parameters = {**run.parameters, "error": str(error), "log_sha256": _hash_file(log_path) if log_path.exists() else None}
        db.commit()
        raise HTTPException(503, f"无法启动 S2 隔离环境：{error}") from error
    except Exception as error:
        db.rollback()
        run = db.get(AnalysisRun, run_id)
        if run is not None:
            run.status = "failed"
            run.parameters = {**run.parameters, "error": f"S2 运行器异常：{type(error).__name__}: {error}", "log_sha256": _hash_file(log_path) if log_path.exists() else None}
            db.commit()
        raise HTTPException(500, "S2 运行器发生未处理异常；AnalysisRun 与日志已保留") from error

    result_dir = _s2_result_path(project_id, run_id)
    manifest_path = result_dir / "run_manifest.json"
    if completed.returncode != 0 or not manifest_path.is_file():
        run.status = "failed"
        run.parameters = {**run.parameters, "error": f"S2 CLI 退出码 {completed.returncode}；详见运行日志", "return_code": completed.returncode, "log_sha256": _hash_file(log_path)}
        db.commit()
        raise HTTPException(422, f"S2 CLI 运行失败（退出码 {completed.returncode}）；未生成正式结果，日志已保留")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        run.status = "failed"
        run.parameters = {**run.parameters, "error": "S2 CLI 运行清单无法解析", "log_sha256": _hash_file(log_path)}
        db.commit()
        raise HTTPException(422, "S2 CLI 运行清单无法解析；日志已保留") from error
    if manifest.get("status") != "completed" or manifest.get("analysis_run_id") != run_id or manifest.get("project_id") != project_id:
        run.status = "failed"
        run.parameters = {**run.parameters, "error": manifest.get("reason", "S2 CLI manifest status/ID 无效"), "log_sha256": _hash_file(log_path)}
        db.commit()
        raise HTTPException(422, "S2 CLI 未完成或输出身份核验失败；日志已保留")
    outputs = manifest.get("outputs", {})
    script_hash = _hash_file(script)
    run.status = "completed"
    run.software_versions = manifest.get("software_versions", {})
    run.input_hashes = {**{path: info.get("sha256", "") for path, info in manifest.get("inputs", {}).items()}, "s2_lincs/project_screen.py": script_hash}
    run.parameters = {
        **run.parameters,
        "result_directory": result_dir.relative_to(root).as_posix(),
        "condition_count": manifest.get("qc_summary", {}).get("dose_time_condition_rows", 0),
        "qc_summary": manifest.get("qc_summary", {}),
        "condition_summary_sha256": outputs.get("drug_condition_summary.csv", {}).get("sha256", ""),
        "log_sha256": _hash_file(log_path),
        "script_sha256": script_hash,
    }
    db.commit()
    try:
        imported = import_project_s2(project_id, S2ImportRequest(analysis_run_id=run_id), db)
    except HTTPException as error:
        run.status = "failed"
        run.parameters = {**run.parameters, "error": f"S2 结果导入失败：{error.detail}"}
        db.commit()
        raise
    return {"project_id": project_id, "analysis_run_id": run_id, "status": "completed", "test_only": project.status == "test_only", "qc_summary": manifest.get("qc_summary", {}), "condition_count": imported.get("condition_count", 0), "log_path": log_path.relative_to(root).as_posix(), "log_sha256": run.parameters.get("log_sha256")}


@app.get(f"{API}/projects/{{project_id}}/s2/summary")
def project_s2_summary(project_id: str, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    run = _latest_project_s2(project_id, db)
    if run is None:
        return {"project_id": project_id, "available": False, "analysis_run_id": None}
    qc = run.parameters.get("qc_summary", {})
    return {"project_id": project_id, "available": True, "analysis_run_id": run.analysis_run_id, "test_only": bool(run.parameters.get("test_only")), "condition_count": run.parameters.get("condition_count", 0), "signature_count": qc.get("scored_signatures", 0), "drug_count": qc.get("scored_drugs", 0), "time": qc.get("time"), "cell_id": qc.get("cell_id"), "perturbation_type": qc.get("pert_type"), "quality_threshold": qc.get("quality_threshold"), "dose_policy": qc.get("dose_policy"), "rank_policy": qc.get("rank_policy")}


@app.get(f"{API}/projects/{{project_id}}/s2/conditions")
def project_s2_conditions(project_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    run = _latest_project_s2(project_id, db)
    if run is None:
        return {"project_id": project_id, "available": False, "analysis_run_id": None, "items": [], "offset": offset, "limit": limit, "total": 0}
    persisted_query = select(S2ConditionResult).where(S2ConditionResult.project_id == project_id, S2ConditionResult.analysis_run_id == run.analysis_run_id).order_by(S2ConditionResult.condition_key, S2ConditionResult.rank_within_condition, S2ConditionResult.pert_id)
    persisted_rows = list(db.scalars(persisted_query.offset(offset).limit(limit)))
    persisted_total = db.scalar(select(func.count()).select_from(S2ConditionResult).where(S2ConditionResult.project_id == project_id, S2ConditionResult.analysis_run_id == run.analysis_run_id)) or 0
    if persisted_total:
        return {"project_id": project_id, "available": True, "analysis_run_id": run.analysis_run_id, "test_only": bool(run.parameters.get("test_only")), "items": [row.condition_json for row in persisted_rows], "offset": offset, "limit": limit, "total": persisted_total, "truncated": offset + len(persisted_rows) < persisted_total, "storage": "database"}
    result_dir = _s2_result_path(project_id, run.analysis_run_id)
    path = result_dir / "drug_condition_summary.csv"
    if not path.is_file():
        raise HTTPException(503, "已登记的项目级 S2 条件汇总文件缺失")
    if _hash_file(path) != run.parameters.get("condition_summary_sha256"):
        raise HTTPException(409, "项目级 S2 条件汇总哈希不匹配")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows.sort(key=lambda row: (row.get("time", ""), row.get("cell_id", ""), row.get("pert_type", ""), row.get("dose", ""), float(row.get("rank_within_condition") or 0), row.get("pert_id", "")))
    return {"project_id": project_id, "available": True, "analysis_run_id": run.analysis_run_id, "test_only": bool(run.parameters.get("test_only")), "items": rows[offset:offset + limit], "offset": offset, "limit": limit, "total": len(rows), "truncated": offset + limit < len(rows), "storage": "verified_csv"}


@app.get(f"{API}/projects/{{project_id}}/s2/stability")
def project_s2_stability(project_id: str, top_n: int = Query(10, ge=1, le=100), db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    run = _latest_project_s2(project_id, db)
    if run is None:
        return {"project_id": project_id, "available": False, "analysis_run_id": None, "items": [], "top_n": top_n}
    persisted = list(db.scalars(select(S2ConditionResult).where(
        S2ConditionResult.project_id == project_id,
        S2ConditionResult.analysis_run_id == run.analysis_run_id,
    )))
    if persisted:
        rows = [item.condition_json for item in persisted]
        storage = "database"
    else:
        path = _s2_result_path(project_id, run.analysis_run_id) / "drug_condition_summary.csv"
        if not path.is_file():
            raise HTTPException(503, "已登记的项目级 S2 条件汇总文件缺失")
        if _hash_file(path) != run.parameters.get("condition_summary_sha256"):
            raise HTTPException(409, "项目级 S2 条件汇总哈希不匹配")
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        storage = "verified_csv"
    return {
        "project_id": project_id,
        "available": True,
        "analysis_run_id": run.analysis_run_id,
        "test_only": bool(run.parameters.get("test_only")),
        "top_n": top_n,
        "storage": storage,
        "aggregation_policy": "按细胞/扰动/时间分层统计各剂量内Top N名次复现率；不合并WTCS，不生成药效总排名",
        "items": summarize_condition_stability(rows, top_n=top_n),
    }


@app.get(f"{API}/projects/{{project_id}}/graph/summary")
def graph_summary(project_id: str, db: Session = Depends(get_db), graph_build_id: str | None = None):
    project = db.get(Project, project_id)
    if not project: raise HTTPException(404, "project not found")
    build = _resolve_graph_build(db, project_id, graph_build_id)
    graph_dir = _graph_bundle_dir_for_build(db, build)
    return {"project_id": project_id, "graph_build_id": build.graph_build_id if build else None, **graph_counts(project.slug, build.graph_build_id if build else None, graph_dir)}


@app.get(f"{API}/projects/{{project_id}}/graph/search")
def graph_search(project_id: str, q: str = Query(..., min_length=2, max_length=100), limit: int = Query(20, ge=1, le=50), db: Session = Depends(get_db), graph_build_id: str | None = None):
    project = db.get(Project, project_id)
    if not project: raise HTTPException(404, "project not found")
    build = _resolve_graph_build(db, project_id, graph_build_id)
    graph_dir = _graph_bundle_dir_for_build(db, build)
    return {"project_id": project_id, "graph_build_id": build.graph_build_id if build else None, "query": q, "items": search_nodes(project.slug, q, limit, build.graph_build_id if build else None, graph_dir)}


@app.get(f"{API}/projects/{{project_id}}/graph/subgraph", response_model=SubgraphOut)
def subgraph(project_id: str, node_id: str | None = None, max_depth: int = Query(1, ge=0, le=2), limit: int = Query(100, ge=1, le=200), cursor: int = Query(0, ge=0), db: Session = Depends(get_db), graph_build_id: str | None = None):
    project = db.get(Project, project_id)
    if not project: raise HTTPException(404, "project not found")
    build = _resolve_graph_build(db, project_id, graph_build_id)
    graph_dir = _graph_bundle_dir_for_build(db, build)
    result = get_subgraph(project_id, project.slug, node_id, max_depth, min(limit, 200), cursor, build.graph_build_id if build else None, build.analysis_run_id if build else None, graph_dir)
    result["graph_build_id"] = build.graph_build_id if build else None
    return result


@app.get(f"{API}/projects/{{project_id}}/drugs/{{drug_id}}/mechanisms")
def drug_mechanisms(project_id: str, drug_id: str, db: Session = Depends(get_db), graph_build_id: str | None = None):
    project = db.get(Project, project_id)
    if not project: raise HTTPException(404, "project not found")
    build = _resolve_graph_build(db, project_id, graph_build_id)
    graph_dir = _graph_bundle_dir_for_build(db, build)
    return {"project_id": project_id, "graph_build_id": build.graph_build_id if build else None, "pert_id": drug_id, "routes": mechanism_for_pert_id(drug_id, project.slug, build.graph_build_id if build else None, graph_dir), "interpretation": "结果包含公共知识图谱路径，以及药物靶点与本项目 S1 富集通路的证据交汇。它们表示关联和证据共现，不构成因果、治疗效果或 ER+ 特异性机制证明。"}


def _graph_build_queryable(build: GraphBuild) -> bool:
    """Neo4j builds are usable only after a complete import; CSV snapshots remain valid."""
    if build.graph_source == "neo4j":
        return build.status == "materialized"
    return build.status in {"materialized", "materialized_snapshot"}


def _mark_graph_build_failed(db: Session, record: GraphBuild, error_message: str) -> None:
    """Record a failed attempt without downgrading a build another request completed."""
    db.refresh(record)
    if record.status == "materialized":
        return
    record.status = "failed"
    record.summary_json = {**(record.summary_json or {}), "neo4j_membership": "failed", "import_error": error_message[-2000:]}
    db.commit()


def _resolve_graph_build(db: Session, project_id: str, requested_id: str | None) -> GraphBuild | None:
    if requested_id:
        build = db.scalar(select(GraphBuild).where(GraphBuild.project_id == project_id, GraphBuild.graph_build_id == requested_id))
        if build is None:
            raise HTTPException(404, "graph build not found in this project")
        if not _graph_build_queryable(build):
            raise HTTPException(409, "graph build is not complete; choose a materialized version")
        return build
    build = db.scalar(
        select(GraphBuild)
        .where(
            GraphBuild.project_id == project_id,
            or_(
                and_(GraphBuild.graph_source == "neo4j", GraphBuild.status == "materialized"),
                and_(GraphBuild.graph_source != "neo4j", GraphBuild.status.in_(["materialized", "materialized_snapshot"])),
            ),
        )
        .order_by(GraphBuild.created_at.desc(), GraphBuild.graph_build_id.desc())
        .limit(1)
    )
    if build is None and settings.graph_source == "neo4j":
        raise HTTPException(409, "no completed graph build is available for this project")
    return build


def _graph_bundle_dir_for_build(db: Session, build: GraphBuild | None, repo_root: Path | None = None) -> str | None:
    """Return a verified project CSV bundle directory for offline graph queries."""
    if build is None or settings.graph_source == "neo4j":
        return None
    run = db.get(AnalysisRun, build.analysis_run_id)
    if run is None or run.run_type != "kg_graph_bundle":
        return None
    relative_dir = run.parameters.get("bundle_dir")
    if not isinstance(relative_dir, str):
        raise HTTPException(409, "GraphBundle AnalysisRun 缺少 bundle_dir")
    repo_root = repo_root or Path(__file__).resolve().parents[2]
    bundle_dir = (repo_root / relative_dir).resolve()
    project_root = (repo_root / "data" / "processed" / "projects" / build.project_id).resolve()
    if not bundle_dir.is_relative_to(project_root):
        raise HTTPException(422, "GraphBundle 查询路径越出项目目录")
    if not build.input_hashes:
        raise HTTPException(409, "GraphBuild 缺少已校验输入文件哈希")
    for relative_name, expected_hash in build.input_hashes.items():
        path = _safe_bundle_path(bundle_dir, relative_name)
        if not path.is_file() or _hash_file(path) != expected_hash:
            raise HTTPException(409, f"GraphBuild 查询输入哈希校验失败：{relative_name}")
    return str(bundle_dir)


def _persist_graph_evidence_records(db: Session, graph_build: GraphBuild, bundle_dir: Path) -> int:
    """Copy hash-verified source assertions into the GraphBuild-scoped SQL store."""
    path = bundle_dir / "evidence_records.csv"
    if not path.is_file():
        raise HTTPException(409, "已验证 GraphBundle 缺少 evidence_records.csv")
    expected_hash = graph_build.input_hashes.get(path.name)
    actual_hash = _hash_file(path)
    if expected_hash is None or actual_hash != expected_hash:
        raise HTTPException(409, "EvidenceRecord 输入哈希与 GraphBuild 登记不一致")

    previous_field_limit = csv.field_size_limit()
    csv.field_size_limit(min(sys.maxsize, 2_147_483_647))
    existing = {
        evidence_id: assertion_hash
        for evidence_id, assertion_hash in db.execute(
            select(EvidenceRecord.evidence_id, EvidenceRecord.assertion_sha256).where(
                EvidenceRecord.graph_build_id == graph_build.graph_build_id
            )
        ).all()
    }
    batch: list[dict] = []
    seen: set[str] = set()
    row_count = 0
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                evidence_id = row.get("evidence_id", "").strip()
                source_assertion_raw = row.get("source_assertion_json", "")
                if len(evidence_id) != 64 or not source_assertion_raw:
                    raise HTTPException(422, "EvidenceRecord 中包含无效 evidence_id 或空断言")
                if evidence_id in seen:
                    raise HTTPException(422, f"EvidenceRecord evidence_id 重复：{evidence_id}")
                seen.add(evidence_id)
                row_count += 1
                assertion_sha256 = hashlib.sha256(source_assertion_raw.encode("utf-8")).hexdigest()
                if evidence_id in existing:
                    if existing[evidence_id] != assertion_sha256:
                        raise HTTPException(409, f"已有 EvidenceRecord 与当前版本断言不一致：{evidence_id}")
                    continue
                try:
                    source_assertion = json.loads(source_assertion_raw)
                except json.JSONDecodeError as error:
                    raise HTTPException(422, f"EvidenceRecord 断言 JSON 无效：{evidence_id}") from error
                if not isinstance(source_assertion, dict):
                    raise HTTPException(422, f"EvidenceRecord 断言必须是 JSON object：{evidence_id}")
                payload = {
                    "record_id": hashlib.sha256(f"{graph_build.graph_build_id}|{evidence_id}".encode()).hexdigest(),
                    "graph_build_id": graph_build.graph_build_id,
                    "project_id": graph_build.project_id,
                    "evidence_id": evidence_id,
                    "source_release": row.get("source_release", ""),
                    "source_record_id": row.get("source_record_id", ""),
                    "subject_id": row.get("subject_id", ""),
                    "predicate": row.get("predicate", ""),
                    "object_id": row.get("object_id", ""),
                    "source_assertion": source_assertion,
                    "source_url": source_assertion.get("source_urls", source_assertion.get("source_url")),
                    "assertion_sha256": assertion_sha256,
                }
                if any(not payload[key] for key in ("source_release", "subject_id", "predicate", "object_id")):
                    raise HTTPException(422, f"EvidenceRecord 关键来源字段缺失：{evidence_id}")
                batch.append(payload)
                if len(batch) == 250:
                    db.execute(insert(EvidenceRecord), batch)
                    batch.clear()
    finally:
        csv.field_size_limit(previous_field_limit)
    missing = set(existing).difference(seen)
    if missing:
        raise HTTPException(409, f"已有 EvidenceRecord 不存在于当前输入：{next(iter(missing))}")
    if batch:
        db.execute(insert(EvidenceRecord), batch)
    db.flush()
    return row_count


def _persist_disease_contexts(db: Session, graph_build: GraphBuild, bundle_dir: Path) -> int:
    """Persist DiseaseContext nodes from the hash-verified graph snapshot."""
    path = bundle_dir / "subgraph_nodes.csv"
    expected_hash = graph_build.input_hashes.get(path.name)
    if expected_hash is None or _hash_file(path) != expected_hash:
        raise HTTPException(409, "DiseaseContext 输入哈希与 GraphBuild 登记不一致")
    existing = {
        context_id: context_sha
        for context_id, context_sha in db.execute(
            select(DiseaseContext.context_id, DiseaseContext.context_sha256).where(
                DiseaseContext.graph_build_id == graph_build.graph_build_id
            )
        ).all()
    }
    batch: list[dict] = []
    seen: set[str] = set()
    count = 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("type") != "DiseaseContext":
                continue
            context_id = row.get("id", "").strip()
            raw = row.get("properties_json", "")
            if not context_id or not raw or context_id in seen:
                raise HTTPException(422, "DiseaseContext 节点 ID 缺失、属性为空或重复")
            seen.add(context_id)
            count += 1
            try:
                properties = json.loads(raw)
            except json.JSONDecodeError as error:
                raise HTTPException(422, f"DiseaseContext 属性 JSON 无效：{context_id}") from error
            if not isinstance(properties, dict) or properties.get("project_id") != graph_build.project_id:
                raise HTTPException(422, f"DiseaseContext 不属于当前项目：{context_id}")
            context_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            if context_id in existing:
                if existing[context_id] != context_sha:
                    raise HTTPException(409, f"已有 DiseaseContext 与当前 GraphBuild 不一致：{context_id}")
                continue
            batch.append({
                "record_id": hashlib.sha256(f"{graph_build.graph_build_id}|{context_id}".encode()).hexdigest(),
                "graph_build_id": graph_build.graph_build_id,
                "project_id": graph_build.project_id,
                "context_id": context_id,
                "disease_id": properties.get("disease_id", ""),
                "context_label": row.get("label", ""),
                "context_json": properties,
                "context_sha256": context_sha,
            })
            if len(batch) >= 250:
                db.execute(insert(DiseaseContext), batch)
                batch.clear()
    missing = set(existing).difference(seen)
    if missing:
        raise HTTPException(409, f"已有 DiseaseContext 不存在于当前 GraphBuild：{next(iter(missing))}")
    if batch:
        db.execute(insert(DiseaseContext), batch)
    db.flush()
    return count


@app.get(f"{API}/evidence/{{evidence_id}}")
def evidence(evidence_id: str, db: Session = Depends(get_db), graph_build_id: str | None = None):
    build = db.get(GraphBuild, graph_build_id) if graph_build_id else None
    if graph_build_id and build is None:
        raise HTTPException(404, "graph build not found")
    if build is not None and not _graph_build_queryable(build):
        raise HTTPException(409, "graph build is not complete; choose a materialized version")
    if build is not None:
        persisted = db.scalar(select(EvidenceRecord).where(
            EvidenceRecord.graph_build_id == build.graph_build_id,
            EvidenceRecord.evidence_id == evidence_id,
        ))
        if persisted is not None:
            return {
                "evidence_id": persisted.evidence_id,
                "graph_build_id": persisted.graph_build_id,
                "source_release": persisted.source_release,
                "source_record_id": persisted.source_record_id,
                "subject_id": persisted.subject_id,
                "predicate": persisted.predicate,
                "object_id": persisted.object_id,
                "source_assertion": persisted.source_assertion,
                "source_url": persisted.source_url,
                "assertion_sha256": persisted.assertion_sha256,
            }
    graph_dir = _graph_bundle_dir_for_build(db, build)
    record = get_evidence(evidence_id, graph_build_id, graph_dir)
    if not record: raise HTTPException(404, "evidence assertion not found")
    return record


@app.post(f"{API}/datasets", response_model=DatasetOut, status_code=201)
def register_dataset(payload: DatasetCreate, db: Session = Depends(get_db)):
    project_id = payload.project_id
    if not db.get(Project, project_id): raise HTTPException(404, "project not found")
    if payload.study_id:
        study = db.get(Study, payload.study_id)
        if study is None or study.project_id != project_id: raise HTTPException(422, "study_id must refer to a study in this project")
    dataset = Dataset(project_id=project_id, study_id=payload.study_id, source=payload.source, release=payload.release, source_url=payload.source_url, sha256=payload.sha256, license_status=payload.license_status, state="validation_pending", metadata_json=payload.metadata_json)
    db.add(dataset); db.commit(); db.refresh(dataset)
    return dataset


@app.post(f"{API}/projects/{{project_id}}/datasets/upload", status_code=201, openapi_extra={"requestBody": {"required": True, "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}})
async def upload_dataset(project_id: str, request: Request, filename: str = Query(..., min_length=1, max_length=255), dataset_type: Literal["expression_matrix", "precomputed_deg", "sample_metadata"] = Query(...), release: str = Query("user-upload", max_length=100), study_id: str | None = Query(None, min_length=1, max_length=36), db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project: raise HTTPException(404, "project not found")
    if study_id:
        study = db.get(Study, study_id)
        if study is None or study.project_id != project_id: raise HTTPException(422, "study_id must refer to a study in this project")
    safe_filename = PurePosixPath(filename.replace("\\", "/")).name
    suffix = Path(safe_filename).suffix.lower()
    if suffix not in {".csv", ".tsv", ".txt"}:
        raise HTTPException(415, "only uncompressed CSV, TSV, or TXT files are accepted")
    root = Path(settings.data_upload_dir)
    if not root.is_absolute(): root = Path(__file__).resolve().parents[2] / root
    dataset_id = str(uuid4())
    directory = root / project.project_id / dataset_id
    directory.mkdir(parents=True, exist_ok=True)
    temporary_path = directory / f"upload{suffix}.part"
    stored_path = directory / f"data{suffix}"
    try:
        file_bytes, sha256 = await write_request_stream(request, temporary_path, settings.max_upload_bytes)
        validation = inspect_expression_file(temporary_path, dataset_type)
        temporary_path.replace(stored_path)
        try: relative_path = stored_path.relative_to(Path(__file__).resolve().parents[2]).as_posix()
        except ValueError: relative_path = stored_path.relative_to(root).as_posix()
        storage_path = relative_path if stored_path.resolve().is_relative_to(Path(__file__).resolve().parents[2]) else str(stored_path.resolve())
        metadata = {**validation, "original_filename": safe_filename, "storage_path": storage_path, "file_bytes": file_bytes, "upload_validated": True}
        dataset = Dataset(dataset_id=dataset_id, project_id=project.project_id, study_id=study_id, source="user_upload", release=release, source_url="", sha256=sha256, license_status="pending_review", state="validation_pending", metadata_json=metadata)
        db.add(dataset); db.commit(); db.refresh(dataset)
        result = DatasetOut.model_validate(dataset).model_dump(mode="json")
        return {**result, "filename": safe_filename, "file_bytes": file_bytes, "dataset_type": dataset_type, "validation": validation, "analysis_started": False}
    except ValueError as error:
        db.rollback()
        temporary_path.unlink(missing_ok=True); stored_path.unlink(missing_ok=True)
        status_code = 413 if "exceeds configured limit" in str(error) else 422
        raise HTTPException(status_code, str(error)) from error
    except Exception:
        db.rollback()
        temporary_path.unlink(missing_ok=True); stored_path.unlink(missing_ok=True)
        raise


@app.post(f"{API}/projects/{{project_id}}/datasets/{{dataset_id}}/s1/precomputed")
def run_precomputed_s1(project_id: str, dataset_id: str, payload: PrecomputedS1Request, db: Session = Depends(get_db)):
    """Register a new S1 run from a validated, already computed DEG table."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.project_id != project_id:
        raise HTTPException(404, "dataset not found in this project")
    if dataset.metadata_json.get("dataset_type") != "precomputed_deg":
        raise HTTPException(409, "该数据集不是预计算差异表达表")
    if dataset.metadata_json.get("upload_validated") is not True:
        raise HTTPException(409, "该文件未通过平台上传结构校验")
    if dataset.state not in {"validation_pending", "validated"}:
        raise HTTPException(409, "该数据集状态不允许运行 S1")
    repo_root = Path(__file__).resolve().parents[2]
    relative_upload = dataset.metadata_json.get("storage_path")
    if not isinstance(relative_upload, str):
        raise HTTPException(409, "数据集缺少受控存储路径")
    stored_source = Path(relative_upload)
    source = (stored_source if stored_source.is_absolute() else repo_root / stored_source).resolve()
    upload_root = Path(settings.data_upload_dir)
    if not upload_root.is_absolute():
        upload_root = repo_root / upload_root
    upload_root = (upload_root / project_id / dataset_id).resolve()
    if not source.is_relative_to(upload_root) or not source.is_file():
        raise HTTPException(409, "数据集文件不在项目上传目录中或已丢失")
    if len(dataset.sha256) != 64 or _hash_file(source) != dataset.sha256:
        raise HTTPException(409, "上传文件 SHA-256 与登记值不一致")

    run_id = f"ANALYSIS_RUN:S1_PRECOMPUTED:{uuid4()}"
    run = AnalysisRun(
        analysis_run_id=run_id,
        project_id=project_id,
        run_type="s1_precomputed_deg",
        status="running",
        parameters={},
        software_versions={"pipeline": "DiseaseGraph precomputed DEG parser 1.0", "python": sys.version.split()[0]},
        input_hashes={dataset.dataset_id: dataset.sha256},
    )
    db.add(run)
    output_path = _precomputed_s1_path(project_id, run)
    try:
        summary = standardize_precomputed_deg(source, output_path, positive_group=payload.positive_group, negative_group=payload.negative_group, fdr_threshold=payload.fdr_threshold)
        output_sha256 = _hash_file(output_path)
        summary.update({"dataset_id": dataset.dataset_id, "input_sha256": dataset.sha256, "output_sha256": output_sha256, "analysis_run_id": run_id, "test_only": project.status == "test_only"})
        run.parameters = summary
        run.status = "completed"
        dataset.state = "validated"
        dataset.metadata_json = {**dataset.metadata_json, "s1_analysis_run_id": run_id, "s1_output_sha256": output_sha256}
        manifest_path = output_path.parent / "run_manifest.json"
        manifest_path.write_text(json.dumps({"analysis_run_id": run_id, "project_id": project_id, "run_type": run.run_type, "status": run.status, "parameters": summary, "software_versions": run.software_versions, "input_hashes": run.input_hashes}, ensure_ascii=False, indent=2), encoding="utf-8")
        db.commit()
    except ValueError as error:
        db.rollback()
        output_path.unlink(missing_ok=True)
        (output_path.parent / "run_manifest.json").unlink(missing_ok=True)
        raise HTTPException(422, str(error)) from error
    except Exception:
        db.rollback()
        output_path.unlink(missing_ok=True)
        (output_path.parent / "run_manifest.json").unlink(missing_ok=True)
        raise
    return {"project_id": project_id, "dataset_id": dataset_id, "analysis_run_id": run_id, "status": "completed", "dataset_state": dataset.state, "summary": summary, "result_path": output_path.relative_to(repo_root).as_posix()}


@app.post(f"{API}/projects/{{project_id}}/datasets/{{dataset_id}}/s1/limma-microarray")
def run_limma_s1(project_id: str, dataset_id: str, payload: LimmaMicroarrayS1Request, db: Session = Depends(get_db)):
    """Run limma on a normalized log2 microarray matrix and matched sample metadata."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    expression_dataset = db.get(Dataset, dataset_id)
    sample_dataset = db.get(Dataset, payload.sample_metadata_dataset_id)
    if expression_dataset is None or expression_dataset.project_id != project_id:
        raise HTTPException(404, "expression matrix dataset not found in this project")
    if sample_dataset is None or sample_dataset.project_id != project_id:
        raise HTTPException(404, "sample metadata dataset not found in this project")
    if expression_dataset.metadata_json.get("dataset_type") != "expression_matrix":
        raise HTTPException(409, "指定数据集不是表达矩阵")
    if sample_dataset.metadata_json.get("dataset_type") != "sample_metadata":
        raise HTTPException(409, "样本注释数据集类型必须为 sample_metadata")
    if expression_dataset.metadata_json.get("upload_validated") is not True or sample_dataset.metadata_json.get("upload_validated") is not True:
        raise HTTPException(409, "表达矩阵和样本注释都必须先通过上传校验")

    repo_root = Path(__file__).resolve().parents[2]
    expression_path = _uploaded_dataset_source(expression_dataset, project_id, repo_root)
    sample_path = _uploaded_dataset_source(sample_dataset, project_id, repo_root)
    run_id = f"ANALYSIS_RUN:S1_LIMMA:{uuid4()}"
    run = AnalysisRun(
        analysis_run_id=run_id,
        project_id=project_id,
        run_type="s1_limma_microarray",
        status="running",
        parameters={"dataset_id": dataset_id, "sample_metadata_dataset_id": sample_dataset.dataset_id, "positive_group": payload.positive_group, "negative_group": payload.negative_group, "contrast": f"{payload.positive_group} - {payload.negative_group}", "fdr_threshold": payload.fdr_threshold, "identifier_type": payload.identifier_type, "measurement_type": payload.measurement_type},
        input_hashes={dataset_id: expression_dataset.sha256, sample_dataset.dataset_id: sample_dataset.sha256},
    )
    db.add(run)
    db.commit()
    output_path = _precomputed_s1_path(project_id, run)
    try:
        summary = run_limma_microarray(repo_root, expression_path, sample_path, output_path, payload.positive_group, payload.negative_group, payload.identifier_type, payload.fdr_threshold)
        output_sha256 = _hash_file(output_path)
        summary.update({"analysis_run_id": run_id, "project_id": project_id, "dataset_id": dataset_id, "sample_metadata_dataset_id": sample_dataset.dataset_id, "fdr_threshold": payload.fdr_threshold, "input_hashes": run.input_hashes, "output_sha256": output_sha256, "test_only": project.status == "test_only", "unique_genes": summary.get("tested_features", 0), "significant_genes": summary.get("significant_features", 0), "gene_identifier_column": "gene_id", "duplicate_rows_removed": 0})
        run.parameters = {**run.parameters, **summary, "output_path": output_path.relative_to(repo_root).as_posix()}
        run.software_versions = {"pipeline": "limma microarray plugin 1.0", "R": summary.get("R", "unknown"), "limma": summary.get("limma", "unknown")}
        run.status = "completed"
        expression_dataset.state = "validated"
        sample_dataset.state = "validated"
        expression_dataset.metadata_json = {**expression_dataset.metadata_json, "s1_analysis_run_id": run_id, "s1_output_sha256": output_sha256}
        manifest_path = output_path.parent / "run_manifest.json"
        manifest_path.write_text(json.dumps({"analysis_run_id": run_id, "project_id": project_id, "run_type": run.run_type, "status": run.status, "parameters": run.parameters, "software_versions": run.software_versions, "input_hashes": run.input_hashes}, ensure_ascii=False, indent=2), encoding="utf-8")
        db.commit()
    except RuntimeError as error:
        db.rollback()
        run.status = "failed"
        run.parameters = {**run.parameters, "error": str(error)[-4000:]}
        db.add(run)
        db.commit()
        output_path.unlink(missing_ok=True)
        Path(str(output_path) + ".summary.json").unlink(missing_ok=True)
        raise HTTPException(503 if "环境" in str(error) or "Rscript" in str(error) else 422, str(error)) from error
    except Exception:
        db.rollback()
        output_path.unlink(missing_ok=True)
        Path(str(output_path) + ".summary.json").unlink(missing_ok=True)
        (output_path.parent / "run_manifest.json").unlink(missing_ok=True)
        raise
    return {"project_id": project_id, "analysis_run_id": run_id, "status": "completed", "summary": summary, "result_path": output_path.relative_to(repo_root).as_posix()}


@app.post(f"{API}/projects/{{project_id}}/datasets/{{dataset_id}}/s1/rnaseq-edger-voom")
def run_rnaseq_s1(project_id: str, dataset_id: str, payload: RNASeqS1Request, db: Session = Depends(get_db)):
    """Run edgeR filtering/TMM and limma-voom on raw integer RNA-seq counts."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    counts_dataset = db.get(Dataset, dataset_id)
    sample_dataset = db.get(Dataset, payload.sample_metadata_dataset_id)
    if counts_dataset is None or counts_dataset.project_id != project_id:
        raise HTTPException(404, "RNA-seq counts dataset not found in this project")
    if sample_dataset is None or sample_dataset.project_id != project_id:
        raise HTTPException(404, "sample metadata dataset not found in this project")
    if counts_dataset.metadata_json.get("dataset_type") != "expression_matrix":
        raise HTTPException(409, "指定数据集不是表达矩阵")
    if sample_dataset.metadata_json.get("dataset_type") != "sample_metadata":
        raise HTTPException(409, "样本注释数据集类型必须为 sample_metadata")
    if counts_dataset.metadata_json.get("upload_validated") is not True or sample_dataset.metadata_json.get("upload_validated") is not True:
        raise HTTPException(409, "counts 和样本注释都必须先通过上传校验")

    repo_root = Path(__file__).resolve().parents[2]
    counts_path = _uploaded_dataset_source(counts_dataset, project_id, repo_root)
    sample_path = _uploaded_dataset_source(sample_dataset, project_id, repo_root)
    run_id = f"ANALYSIS_RUN:S1_RNASEQ:{uuid4()}"
    run = AnalysisRun(
        analysis_run_id=run_id,
        project_id=project_id,
        run_type="s1_rnaseq_edger_voom",
        status="running",
        parameters={"dataset_id": dataset_id, "sample_metadata_dataset_id": sample_dataset.dataset_id, "positive_group": payload.positive_group, "negative_group": payload.negative_group, "contrast": f"{payload.positive_group} - {payload.negative_group}", "fdr_threshold": payload.fdr_threshold, "identifier_type": payload.identifier_type, "measurement_type": payload.measurement_type},
        input_hashes={dataset_id: counts_dataset.sha256, sample_dataset.dataset_id: sample_dataset.sha256},
    )
    db.add(run)
    db.commit()
    output_path = _precomputed_s1_path(project_id, run)
    try:
        summary = run_rnaseq_edger_voom(repo_root, counts_path, sample_path, output_path, payload.positive_group, payload.negative_group, payload.identifier_type, payload.fdr_threshold)
        output_sha256 = _hash_file(output_path)
        summary.update({"analysis_run_id": run_id, "project_id": project_id, "dataset_id": dataset_id, "sample_metadata_dataset_id": sample_dataset.dataset_id, "fdr_threshold": payload.fdr_threshold, "input_hashes": run.input_hashes, "output_sha256": output_sha256, "test_only": project.status == "test_only", "unique_genes": summary.get("tested_features", 0), "significant_genes": summary.get("significant_features", 0), "gene_identifier_column": "gene_id", "duplicate_rows_removed": 0})
        run.parameters = {**run.parameters, **summary, "output_path": output_path.relative_to(repo_root).as_posix()}
        run.software_versions = {"pipeline": "edgeR filterByExpr + TMM + limma-voom 1.0", "R": summary.get("R", "unknown"), "edgeR": summary.get("edgeR", "unknown"), "limma": summary.get("limma", "unknown")}
        run.status = "completed"
        counts_dataset.state = "validated"
        sample_dataset.state = "validated"
        counts_dataset.metadata_json = {**counts_dataset.metadata_json, "s1_analysis_run_id": run_id, "s1_output_sha256": output_sha256}
        manifest_path = output_path.parent / "run_manifest.json"
        manifest_path.write_text(json.dumps({"analysis_run_id": run_id, "project_id": project_id, "run_type": run.run_type, "status": run.status, "parameters": run.parameters, "software_versions": run.software_versions, "input_hashes": run.input_hashes}, ensure_ascii=False, indent=2), encoding="utf-8")
        db.commit()
    except RuntimeError as error:
        db.rollback()
        run.status = "failed"
        run.parameters = {**run.parameters, "error": str(error)[-4000:]}
        db.add(run)
        db.commit()
        output_path.unlink(missing_ok=True)
        Path(str(output_path) + ".summary.json").unlink(missing_ok=True)
        raise HTTPException(503 if "环境" in str(error) or "Rscript" in str(error) else 422, str(error)) from error
    except Exception:
        db.rollback()
        output_path.unlink(missing_ok=True)
        Path(str(output_path) + ".summary.json").unlink(missing_ok=True)
        (output_path.parent / "run_manifest.json").unlink(missing_ok=True)
        raise
    return {"project_id": project_id, "analysis_run_id": run_id, "status": "completed", "summary": summary, "result_path": output_path.relative_to(repo_root).as_posix()}


@app.post(f"{API}/projects/{{project_id}}/s1/pathway-enrichment")
def run_project_pathway_enrichment(project_id: str, payload: S1EnrichmentRequest, db: Session = Depends(get_db)):
    """Run GO BP, Reactome ORA and Hallmark GSEA for a completed project S1 run."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    s1_run = db.get(AnalysisRun, payload.s1_analysis_run_id)
    if s1_run is None or s1_run.project_id != project_id or s1_run.run_type not in {"s1_precomputed_deg", "s1_limma_microarray", "s1_rnaseq_edger_voom"} or s1_run.status != "completed":
        raise HTTPException(409, "请选择本项目已完成的 S1 分析运行")
    repo_root = Path(__file__).resolve().parents[2]
    s1_path = _precomputed_s1_path(project_id, s1_run)
    if not s1_path.is_file():
        raise HTTPException(503, "登记的 S1 结果文件缺失")
    expected_hash = s1_run.parameters.get("output_sha256")
    s1_hash = _hash_file(s1_path)
    if expected_hash and expected_hash != s1_hash:
        raise HTTPException(409, "S1 结果文件校验值与 AnalysisRun 记录不一致")
    contrast = str(s1_run.parameters.get("contrast") or f"{s1_run.parameters.get('positive_group', 'positive')} - {s1_run.parameters.get('negative_group', 'negative')}")
    run_id = f"ANALYSIS_RUN:S1_ENRICHMENT:{uuid4()}"
    run_token = run_id.rsplit(":", 1)[-1]
    result_dir = (repo_root / "data" / "processed" / "projects" / project_id / "s1_enrichment" / run_token).resolve()
    project_root = (repo_root / "data" / "processed" / "projects" / project_id).resolve()
    if not result_dir.is_relative_to(project_root):
        raise HTTPException(422, "S1 富集结果路径越界")
    script = Path(__file__).parent / "pipelines" / "project_s1_enrichment.R"
    gmt = repo_root / "experiments" / "s1_pathway_enrichment" / "input" / "h.all.v2025.1.Hs.entrez.gmt"
    run = AnalysisRun(analysis_run_id=run_id, project_id=project_id, run_type="s1_pathway_enrichment", status="running", parameters={"s1_analysis_run_id": s1_run.analysis_run_id, "identifier_type": payload.identifier_type, "fdr_threshold": payload.fdr_threshold, "contrast": contrast, "test_only": project.status == "test_only"}, software_versions={}, input_hashes={s1_run.analysis_run_id: s1_hash})
    db.add(run)
    db.commit()
    try:
        summary = run_project_s1_enrichment(repo_root, s1_path, result_dir, payload.identifier_type, contrast, payload.fdr_threshold)
        output_names = ["gene_mapping_audit.csv", "s1_up_genes.csv", "s1_down_genes.csv", "go_up.csv", "go_down.csv", "reactome_up.csv", "reactome_down.csv", "hallmark_gsea.csv", "pathway_gene_links.csv", "summary.json", "enrichment_report.md"]
        output_hashes = {name: _hash_file(result_dir / name) for name in output_names}
        figures = sorted(path.name for path in (result_dir / "figures").glob("*.png"))
        script_hash = _hash_file(script)
        gmt_hash = _hash_file(gmt) if gmt.is_file() else "missing"
        sig_counts = {"go_up": int(summary.get("go_up_significant", 0)), "go_down": int(summary.get("go_down_significant", 0)), "reactome_up": int(summary.get("reactome_up_significant", 0)), "reactome_down": int(summary.get("reactome_down_significant", 0)), "hallmark_gsea": int(summary.get("hallmark_significant", 0))}
        run.parameters = {**run.parameters, **summary, "significant_pathway_counts": sig_counts, "output_dir": result_dir.relative_to(repo_root).as_posix(), "output_hashes": output_hashes, "figures": figures, "report_path": (result_dir / "enrichment_report.md").relative_to(repo_root).as_posix()}
        run.software_versions = {"pipeline": "DiseaseGraph project S1 pathway enrichment 1.0", "R": summary.get("r_version", "unknown"), **summary.get("packages", {})}
        run.input_hashes = {s1_run.analysis_run_id: s1_hash, "pipeline_script_sha256": script_hash, "hallmark_gmt_sha256": gmt_hash}
        run.status = "completed"
        (result_dir / "run_manifest.json").write_text(json.dumps({"analysis_run_id": run_id, "project_id": project_id, "s1_analysis_run_id": s1_run.analysis_run_id, "run_type": run.run_type, "status": run.status, "parameters": run.parameters, "software_versions": run.software_versions, "input_hashes": run.input_hashes}, ensure_ascii=False, indent=2), encoding="utf-8")
        db.commit()
    except Exception as error:
        db.rollback()
        run = db.get(AnalysisRun, run_id)
        if run is not None:
            run.status = "failed"
            run.parameters = {**run.parameters, "error": str(error)[-5000:], "output_dir": result_dir.relative_to(repo_root).as_posix()}
            db.commit()
        status = 503 if "环境" in str(error) or "Rscript" in str(error) else 422
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(status, str(error)) from error
    return {"project_id": project_id, "analysis_run_id": run_id, "s1_analysis_run_id": s1_run.analysis_run_id, "status": "completed", "summary": summary, "figures": figures, "result_dir": result_dir.relative_to(repo_root).as_posix()}


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_kg_python(repo_root: Path) -> Path | None:
    """Use the isolated KG ETL runtime, which owns BioCypher and graph dependencies."""
    candidates = [
        repo_root / "kg" / ".venv" / "Scripts" / "python.exe",
        repo_root / "kg" / ".venv" / "bin" / "python",
        repo_root / "s2_lincs" / ".venv" / "Scripts" / "python.exe",
        repo_root / "s2_lincs" / ".venv" / "bin" / "python",
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _safe_bundle_path(bundle_root: Path, relative_path: str) -> Path:
    root = bundle_root.resolve()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(422, "图谱输入包路径越界")
    return candidate


@app.post(f"{API}/projects/{{project_id}}/graph-bundles")
def build_project_graph_bundle(project_id: str, payload: GraphBundleRequest, db: Session = Depends(get_db)):
    """Extract a versioned, auditable project subgraph from linked S1/S2/enrichment runs."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    s1_run = db.get(AnalysisRun, payload.s1_analysis_run_id)
    s2_run = db.get(AnalysisRun, payload.s2_analysis_run_id)
    enrichment_run = db.get(AnalysisRun, payload.enrichment_analysis_run_id)
    valid_s1_types = {"s1_precomputed_deg", "s1_limma_microarray", "s1_rnaseq_edger_voom"}
    if s1_run is None or s1_run.project_id != project_id or s1_run.run_type not in valid_s1_types or s1_run.status != "completed":
        raise HTTPException(409, "GraphBundle 需要本项目已完成的 S1 运行")
    if s2_run is None or s2_run.project_id != project_id or s2_run.run_type != "s2_lincs" or s2_run.status != "completed":
        raise HTTPException(409, "GraphBundle 需要本项目已完成的 S2 LINCS 运行")
    if enrichment_run is None or enrichment_run.project_id != project_id or enrichment_run.run_type != "s1_pathway_enrichment" or enrichment_run.status != "completed":
        raise HTTPException(409, "GraphBundle 需要本项目已完成的 S1 通路富集运行")
    if s2_run.parameters.get("s1_analysis_run_id") != s1_run.analysis_run_id:
        raise HTTPException(409, "所选 S2 运行不是基于所选 S1 运行")
    if enrichment_run.parameters.get("s1_analysis_run_id") != s1_run.analysis_run_id:
        raise HTTPException(409, "所选富集运行不是基于所选 S1 运行")
    expected_test_only = project.status == "test_only"
    if bool(s2_run.parameters.get("test_only")) != expected_test_only or bool(enrichment_run.parameters.get("test_only")) != expected_test_only:
        raise HTTPException(409, "科研运行与 test_only 项目标记不一致")

    repo_root = Path(__file__).resolve().parents[2]
    project_data_root = (repo_root / "data" / "processed" / "projects" / project_id).resolve()
    s1_path = _precomputed_s1_path(project_id, s1_run)
    if not s1_path.is_file():
        raise HTTPException(503, "S1 差异结果文件缺失")
    s1_hash = _hash_file(s1_path)
    if s1_run.parameters.get("output_sha256") and s1_run.parameters["output_sha256"] != s1_hash:
        raise HTTPException(409, "S1 输出 SHA-256 与 AnalysisRun 不一致")
    s2_result_relative = s2_run.parameters.get("result_directory")
    if not isinstance(s2_result_relative, str):
        raise HTTPException(409, "S2 AnalysisRun 缺少结果目录 provenance")
    s2_result_dir = (repo_root / s2_result_relative).resolve()
    if not s2_result_dir.is_relative_to(project_data_root):
        raise HTTPException(422, "S2 结果目录越出项目数据边界")
    s2_conditions = s2_result_dir / "drug_condition_summary.csv"
    if not s2_conditions.is_file() or _hash_file(s2_conditions) != s2_run.parameters.get("condition_summary_sha256"):
        raise HTTPException(409, "S2 条件结果缺失或哈希校验失败")
    enrichment_relative = enrichment_run.parameters.get("output_dir")
    if not isinstance(enrichment_relative, str):
        raise HTTPException(409, "通路富集 AnalysisRun 缺少结果目录 provenance")
    enrichment_dir = (repo_root / enrichment_relative).resolve()
    if not enrichment_dir.is_relative_to(project_data_root):
        raise HTTPException(422, "通路富集结果目录越出项目数据边界")
    expected_enrichment_outputs = enrichment_run.parameters.get("output_hashes", {})
    enrichment_required = ["gene_mapping_audit.csv", "pathway_gene_links.csv", "go_up.csv", "go_down.csv", "reactome_up.csv", "reactome_down.csv", "hallmark_gsea.csv"]
    for name in enrichment_required:
        path = enrichment_dir / name
        if not path.is_file() or expected_enrichment_outputs.get(name) != _hash_file(path):
            raise HTTPException(409, f"富集结果 {name} 缺失或哈希校验失败")
    mapping_path = enrichment_dir / "gene_mapping_audit.csv"
    pert_info_path = repo_root / "s2_lincs" / "data" / "GSE70138_Broad_LINCS_pert_info_2017-03-06.txt.gz"
    if not pert_info_path.is_file():
        raise HTTPException(503, "LINCS perturbagen 结构元数据缺失；无法执行可审计的精确化合物映射")

    run_id = f"ANALYSIS_RUN:KG_GRAPH_BUNDLE:{uuid4()}"
    run_token = run_id.rsplit(":", 1)[-1]
    bundle_dir = (project_data_root / "graph_bundle" / "runs" / run_token).resolve()
    if not bundle_dir.is_relative_to(project_data_root):
        raise HTTPException(422, "GraphBundle 输出路径越界")
    config_path = bundle_dir / "build_config.json"
    manifest_path = bundle_dir / "bundle_manifest.json"
    bundle_run = AnalysisRun(
        analysis_run_id=run_id,
        project_id=project_id,
        run_type="kg_graph_bundle",
        status="running",
        parameters={"s1_analysis_run_id": s1_run.analysis_run_id, "s2_analysis_run_id": s2_run.analysis_run_id, "enrichment_analysis_run_id": enrichment_run.analysis_run_id, "test_only": expected_test_only},
        software_versions={"pipeline": "build_project_graph_bundle.py"},
        input_hashes={s1_run.analysis_run_id: s1_hash, s2_run.analysis_run_id: _hash_file(s2_conditions), "enrichment_analysis_run_id": _hash_file(enrichment_dir / "pathway_gene_links.csv")},
    )
    db.add(bundle_run)
    db.commit()
    config = {
        "project_id": project.project_id, "project_slug": project.slug, "project_name": project.name, "disease_id": project.disease_id,
        "analysis_run_id": run_id, "s1_analysis_run_id": s1_run.analysis_run_id, "s2_analysis_run_id": s2_run.analysis_run_id,
        "enrichment_analysis_run_id": enrichment_run.analysis_run_id, "test_only": expected_test_only,
        "s1_fdr_threshold": float(s1_run.parameters.get("fdr_threshold", 0.05)), "s1_source_release": str(s1_run.parameters.get("dataset_id") or s1_run.parameters.get("dataset") or s1_run.run_type),
        "s1_table": str(s1_path), "s1_mapping_audit": str(mapping_path), "s2_conditions": str(s2_conditions),
        "pert_info_path": str(pert_info_path), "enrichment_dir": str(enrichment_dir), "public_kg_dir": str(repo_root / "data" / "optimuskg" / "3.2"),
        "output_dir": str(bundle_dir), "config_path": str(config_path),
    }
    try:
        bundle_dir.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        script = repo_root / "kg" / "scripts" / "build_project_graph_bundle.py"
        python = _resolve_kg_python(repo_root)
        if python is None or not script.is_file():
            raise FileNotFoundError("GraphBundle Python 环境或构建脚本缺失")
        version_result = subprocess.run([str(python), "--version"], cwd=repo_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=True)
        completed = subprocess.run([str(python), str(script), "--config", str(config_path)], cwd=repo_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"GraphBundle CLI 退出码 {completed.returncode}：{(completed.stderr or completed.stdout)[-5000:]}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "validated" or manifest.get("project_id") != project_id or manifest.get("analysis_run_id") != run_id or bool(manifest.get("test_only")) != expected_test_only:
            raise ValueError("GraphBundle 清单身份、状态或 test_only 标记校验失败")
        if manifest.get("s1_analysis_run_id") != s1_run.analysis_run_id or manifest.get("s2_analysis_run_id") != s2_run.analysis_run_id or manifest.get("enrichment_analysis_run_id") != enrichment_run.analysis_run_id:
            raise ValueError("GraphBundle 清单关联的 S1/S2/富集 AnalysisRun 不一致")
        runtime_versions = manifest.get("software_versions")
        required_runtime_versions = {"python", "duckdb", "polars", "pyarrow", "biocypher"}
        if not isinstance(runtime_versions, dict) or not required_runtime_versions.issubset(runtime_versions):
            raise ValueError("GraphBundle 清单缺少 Python/ETL/BioCypher 软件版本")
        output_hashes = manifest.get("output_hashes", {})
        for filename, expected_hash in output_hashes.items():
            output_file = (bundle_dir / filename).resolve()
            if not output_file.is_relative_to(bundle_dir) or not output_file.is_file() or _hash_file(output_file) != expected_hash:
                raise ValueError(f"GraphBundle 输出文件校验失败：{filename}")
        bundle_checksums = json.loads((bundle_dir / "bundle_checksums.json").read_text(encoding="utf-8"))
        if bundle_checksums.get("bundle_manifest.json") != _hash_file(manifest_path):
            raise ValueError("GraphBundle manifest 校验值不一致")
        bundle_run.status = "completed"
        bundle_run.parameters = {**bundle_run.parameters, "bundle_dir": bundle_dir.relative_to(repo_root).as_posix(), "manifest_sha256": _hash_file(manifest_path), "output_hashes": output_hashes, "counts": manifest.get("counts", {}), "mapping_policy": manifest.get("mapping_policy"), "cross_dose_policy": manifest.get("cross_dose_policy")}
        bundle_run.software_versions = {"pipeline": "project graph bundle 1.0", **runtime_versions, "python_runtime": (version_result.stdout or version_result.stderr).strip(), "platform": platform.platform()}
        bundle_run.input_hashes = {**manifest.get("input_hashes", {}), "builder_script_sha256": _hash_file(script)}
        run_manifest = {"analysis_run_id": run_id, "project_id": project_id, "run_type": bundle_run.run_type, "status": "completed", "parameters": bundle_run.parameters, "software_versions": bundle_run.software_versions, "input_hashes": bundle_run.input_hashes}
        run_manifest_path = bundle_dir / "analysis_run_manifest.json"
        run_manifest_path.write_text(json.dumps(run_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        bundle_run.parameters = {**bundle_run.parameters, "analysis_run_manifest_sha256": _hash_file(run_manifest_path)}
        db.commit()
    except Exception as error:
        db.rollback()
        failed_run = db.get(AnalysisRun, run_id)
        if failed_run is not None:
            failed_run.status = "failed"
            failed_run.parameters = {**failed_run.parameters, "error": str(error)[-5000:], "bundle_dir": bundle_dir.relative_to(repo_root).as_posix()}
            db.commit()
        status = 503 if isinstance(error, OSError) else 422
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(status, str(error)) from error
    return {"project_id": project_id, "analysis_run_id": run_id, "status": "completed", "test_only": expected_test_only, "bundle_dir": bundle_dir.relative_to(repo_root).as_posix(), "manifest_sha256": bundle_run.parameters["manifest_sha256"], "counts": manifest.get("counts", {}), "output_hashes": output_hashes}


def _graph_build_inputs(project: Project, repo_root: Path, db: Session, bundle_run_id: str | None = None):
    """Return (run, subgraph dir, extension dir, input hashes, test_only)."""
    bundle_run = None
    if bundle_run_id:
        bundle_run = db.get(AnalysisRun, bundle_run_id)
        if bundle_run is None or bundle_run.project_id != project.project_id or bundle_run.run_type != "kg_graph_bundle" or bundle_run.status != "completed":
            raise HTTPException(409, "所选 GraphBundle 运行不存在、未完成或不属于当前项目")
    else:
        bundle_run = db.scalar(select(AnalysisRun).where(AnalysisRun.project_id == project.project_id, AnalysisRun.run_type == "kg_graph_bundle", AnalysisRun.status == "completed").order_by(AnalysisRun.created_at.desc(), AnalysisRun.analysis_run_id.desc()))
    if bundle_run is not None:
        bundle_root = repo_root / "data" / "processed" / "projects" / project.project_id / "graph_bundle" / "runs"
        bundle_dir = (bundle_root / bundle_run.analysis_run_id.rsplit(":", 1)[-1]).resolve()
        if not bundle_dir.is_relative_to(bundle_root.resolve()):
            raise HTTPException(422, "GraphBundle 运行目录越出当前项目边界")
        manifest_path = bundle_dir / "bundle_manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(422, "GraphBundle manifest 缺失或无法读取") from error
        if manifest.get("status") != "validated" or manifest.get("project_id") != project.project_id or manifest.get("analysis_run_id") != bundle_run.analysis_run_id:
            raise HTTPException(409, "GraphBundle manifest 与登记运行不一致")
        if bundle_run.parameters.get("manifest_sha256") != _hash_file(manifest_path):
            raise HTTPException(409, "GraphBundle manifest 与 AnalysisRun 登记哈希不一致")
        if bundle_run.parameters.get("output_hashes") != manifest.get("output_hashes"):
            raise HTTPException(409, "GraphBundle 输出哈希与 AnalysisRun 登记内容不一致")
        checksums_path = bundle_dir / "bundle_checksums.json"
        try:
            checksums = json.loads(checksums_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(422, "GraphBundle 校验清单缺失或无法读取") from error
        for filename, expected_hash in manifest.get("output_hashes", {}).items():
            path = (bundle_dir / filename).resolve()
            if not path.is_relative_to(bundle_dir) or not path.is_file() or _hash_file(path) != expected_hash or checksums.get(filename) != expected_hash:
                raise HTTPException(409, f"GraphBundle 文件校验失败：{filename}")
        if checksums.get("bundle_manifest.json") != _hash_file(manifest_path):
            raise HTTPException(409, "GraphBundle manifest 哈希不匹配")
        run_manifest_path = bundle_dir / "analysis_run_manifest.json"
        try:
            analysis_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(422, "GraphBundle AnalysisRun 清单缺失或无法读取") from error
        if bundle_run.parameters.get("analysis_run_manifest_sha256") != _hash_file(run_manifest_path):
            raise HTTPException(409, "GraphBundle AnalysisRun 清单哈希与数据库记录不一致")
        registered_parameters = {key: value for key, value in bundle_run.parameters.items() if key != "analysis_run_manifest_sha256"}
        expected_analysis_manifest = {"analysis_run_id": bundle_run.analysis_run_id, "project_id": project.project_id, "run_type": "kg_graph_bundle", "status": "completed", "parameters": registered_parameters, "software_versions": bundle_run.software_versions, "input_hashes": bundle_run.input_hashes}
        if analysis_manifest != expected_analysis_manifest:
            raise HTTPException(409, "GraphBundle AnalysisRun 清单内容与数据库记录不一致")
        subgraph_dir = bundle_dir
        input_paths = [manifest_path, checksums_path, run_manifest_path, *[bundle_dir / name for name in manifest["output_hashes"]]]
        if any(not path.is_file() for path in input_paths):
            raise HTTPException(422, "GraphBundle 必需输出文件缺失")
        hashes = {path.relative_to(bundle_dir).as_posix(): _hash_file(path) for path in sorted(set(input_paths))}
        return bundle_run, subgraph_dir, None, hashes, bool(manifest.get("test_only", False))

    if project.slug == "erpos-gse42568":
        run = db.get(AnalysisRun, "PHASEB_OPTIMUSKG32_20261113")
        if run is None or run.project_id != project.project_id:
            raise HTTPException(409, "当前项目缺少已登记的 Phase B 分析运行")
        subgraph_dir = Path(settings.graph_data_dir)
        if not subgraph_dir.is_absolute():
            subgraph_dir = (repo_root / subgraph_dir).resolve()
        extension_dir = subgraph_dir.parent / "platform_phase_b_pathways"
        paths = [subgraph_dir / "subgraph_nodes.csv", subgraph_dir / "subgraph_edges.csv", subgraph_dir / "phase_b_manifest.json"]
        paths.extend(extension_dir / name for name in ["pathway_nodes.csv", "gene_nodes.csv", "pathway_edges.csv"])
        if any(not path.is_file() for path in paths):
            raise HTTPException(503, "Phase B 图谱输入文件缺失")
        return run, subgraph_dir, extension_dir, {path.relative_to(repo_root).as_posix(): _hash_file(path) for path in paths}, False

    bundle_root = repo_root / "data" / "processed" / "projects" / project.project_id / "graph_bundle"
    manifest_path = bundle_root / "bundle_manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(409, "该项目没有已验证图谱输入包；请先完成 S1/S2 与实体映射并生成 graph_bundle")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(422, "图谱输入包清单无法读取") from error
    if manifest.get("status") != "validated" or manifest.get("project_id") != project.project_id:
        raise HTTPException(409, "图谱输入包尚未验证或 project_id 不匹配")
    run_id = manifest.get("analysis_run_id")
    run = db.get(AnalysisRun, run_id) if isinstance(run_id, str) else None
    if run is None or run.project_id != project.project_id or run.status != "completed":
        raise HTTPException(409, "图谱输入包没有关联本项目已完成的 AnalysisRun")
    subgraph_dir = bundle_root
    extension_dir = bundle_root / "pathway_extension"
    required = [bundle_root / "subgraph_nodes.csv", bundle_root / "subgraph_edges.csv"]
    paths = [manifest_path, *required]
    if extension_dir.exists():
        paths.extend(path for path in extension_dir.iterdir() if path.is_file() and path.suffix.lower() == ".csv")
    if any(not path.is_file() for path in required):
        raise HTTPException(422, "图谱输入包缺少 subgraph_nodes.csv 或 subgraph_edges.csv")
    hashes = {path.relative_to(bundle_root).as_posix(): _hash_file(path) for path in sorted(set(paths))}
    return run, subgraph_dir, extension_dir if extension_dir.exists() else None, hashes, bool(manifest.get("test_only", False))


@app.post(f"{API}/projects/{{project_id}}/graph-builds")
def graph_build(project_id: str, db: Session = Depends(get_db), bundle_run_id: str | None = None):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")
    repo_root = Path(__file__).resolve().parents[2]
    run, subgraph_dir, extension_dir, hashes, test_only = _graph_build_inputs(project, repo_root, db, bundle_run_id)
    key_payload = {"graph_source": settings.graph_source, "input_hashes": hashes}
    neo4j_importer_sha256 = None
    if settings.graph_source == "neo4j" and run.run_type == "kg_graph_bundle":
        neo4j_importer_sha256 = _hash_file(repo_root / "kg" / "scripts" / "import_phase_b_to_neo4j.py")
        key_payload["neo4j_importer_sha256"] = neo4j_importer_sha256
    if project.slug != "erpos-gse42568":
        key_payload["analysis_run_id"] = run.analysis_run_id
    key_source = json.dumps(key_payload, sort_keys=True)
    build_key = hashlib.sha256(key_source.encode()).hexdigest()
    graph_build_id = f"GRAPH_BUILD:{project_id}:{build_key[:16]}"
    record = db.scalar(select(GraphBuild).where(GraphBuild.project_id == project_id, GraphBuild.build_key == build_key))
    if record is None:
        record = GraphBuild(
            graph_build_id=graph_build_id,
            project_id=project_id,
            analysis_run_id=run.analysis_run_id,
            build_key=build_key,
            status="importing" if settings.graph_source == "neo4j" else "materialized_snapshot",
            graph_source=settings.graph_source,
            input_hashes=hashes,
            summary_json={"snapshot_kind": "已验证项目图谱输入包", "test_only": test_only, "neo4j_importer_sha256": neo4j_importer_sha256},
        )
        db.add(record)
        try:
            db.commit()
            db.refresh(record)
        except IntegrityError:
            db.rollback()
            record = db.scalar(select(GraphBuild).where(GraphBuild.project_id == project_id, GraphBuild.build_key == build_key))
            if record is None:
                raise
    if settings.graph_source == "neo4j" and record.summary_json.get("neo4j_membership") != "registered":
        # Mark the build as importing before preflight/subprocess. A crash or a
        # failed preflight must never leave it queryable as a completed version.
        if record.status != "importing":
            record.status = "importing"
            db.commit()
            db.refresh(record)
        importer = repo_root / "kg" / "scripts" / "import_phase_b_to_neo4j.py"
        child_env = os.environ.copy()
        child_env.update({"NEO4J_URI": settings.neo4j_uri, "NEO4J_USER": settings.neo4j_user, "NEO4J_PASSWORD": settings.neo4j_password, "NEO4J_DATABASE": settings.neo4j_database})
        kg_python = _resolve_kg_python(repo_root)
        if kg_python is None:
            _mark_graph_build_failed(db, record, "KG Python 环境缺失；需要 BioCypher 的隔离 KG venv")
            raise HTTPException(503, "Neo4j 导入所需 KG Python 环境缺失；失败状态已保存")
        command = [
            str(kg_python), str(importer),
            "--graph-build-id", record.graph_build_id,
            "--subgraph-dir", str(subgraph_dir),
            "--project-id", project_id,
            "--build-key", record.build_key,
            "--expected-input-hashes-json", json.dumps(record.input_hashes, sort_keys=True, separators=(",", ":")),
        ]
        command.extend(["--pathway-extension-dir", str(extension_dir or (subgraph_dir / "__no_pathway_extension__"))])
        try:
            result = subprocess.run(command, cwd=repo_root, env=child_env, capture_output=True, text=True, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            _mark_graph_build_failed(db, record, str(error))
            raise HTTPException(503, "Neo4j 图谱版本导入启动失败或超时；失败状态已保存") from error
        if result.returncode:
            _mark_graph_build_failed(db, record, result.stderr)
            raise HTTPException(503, "Neo4j 图谱版本导入失败；失败信息已写入 GraphBuild 记录")
        record.status = "materialized"
        record.summary_json = {**graph_counts(project.slug, record.graph_build_id), "snapshot_kind": "按 GraphBuild ID 标记的 Neo4j 图谱版本", "neo4j_membership": "registered", "import_status": "PASS", "test_only": test_only, "neo4j_importer_sha256": neo4j_importer_sha256}
        db.commit()
        db.refresh(record)
    if run.run_type == "kg_graph_bundle":
        try:
            evidence_count = _persist_graph_evidence_records(db, record, Path(subgraph_dir))
            disease_context_count = _persist_disease_contexts(db, record, Path(subgraph_dir))
            record.summary_json = {
                **record.summary_json,
                "evidence_record_count": evidence_count,
                "evidence_records_sha256": record.input_hashes.get("evidence_records.csv"),
                "evidence_records_status": "persisted",
                "disease_context_count": disease_context_count,
                "disease_contexts_sha256": record.input_hashes.get("subgraph_nodes.csv"),
                "disease_contexts_status": "persisted",
            }
            db.commit()
            db.refresh(record)
        except Exception as error:
            db.rollback()
            failed_evidence_record = db.get(GraphBuild, record.graph_build_id)
            if failed_evidence_record is not None:
                failed_evidence_record.summary_json = {
                    **failed_evidence_record.summary_json,
                    "evidence_records_status": "failed",
                    "evidence_records_error": str(error)[-2000:],
                }
                db.commit()
            if isinstance(error, HTTPException):
                raise
            raise HTTPException(503, "EvidenceRecord 持久化失败；GraphBuild 仍保留，重试请求可幂等补齐") from error
    return {
        "project_id": record.project_id,
        "graph_build_id": record.graph_build_id,
        "analysis_run_id": record.analysis_run_id,
        "status": record.status,
        "graph_source": record.graph_source,
        "created_at": record.created_at,
        "input_hashes": record.input_hashes,
        "summary": record.summary_json,
        "idempotent": True,
        "detail": "已登记并幂等导入该项目的已验证图谱输入包；公共实体按稳定 ID 共用，项目关系按 GraphBuild ID 隔离。",
    }


@app.get(f"{API}/projects/{{project_id}}/graph-builds")
def list_graph_builds(project_id: str, db: Session = Depends(get_db)):
    if not db.get(Project, project_id):
        raise HTTPException(404, "project not found")
    records = db.scalars(select(GraphBuild).where(GraphBuild.project_id == project_id).order_by(GraphBuild.created_at.desc(), GraphBuild.graph_build_id.desc()))
    return {
        "project_id": project_id,
        "items": [
            {
                "graph_build_id": row.graph_build_id,
                "analysis_run_id": row.analysis_run_id,
                "status": row.status,
                "graph_source": row.graph_source,
                "created_at": row.created_at,
                "input_hashes": row.input_hashes,
                "summary": row.summary_json,
            }
            for row in records
        ],
    }
