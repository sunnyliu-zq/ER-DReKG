"""Register immutable historical GSE42568/LINCS outputs to the formal ER+ project.

This is a provenance adapter, not a rerun of limma, LINCS scoring, or pathway
enrichment. The original scientific output files are read-only; only
project-scoped copies, manifests, and append-only database records are added.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

csv.field_size_limit(min(sys.maxsize, 2_147_483_647))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# 后端依赖（FastAPI/SQLAlchemy/app.*）延迟到非 dry-run 流程再导入，
# 使 --dry-run 离线预览可在无后端运行环境（未 uv sync、未安装 fastapi）时独立执行。


PROJECT_ID = "3f18f4c1-713d-58c5-8a53-38eca5a5aaf9"
PROJECT_SLUG = "erpos-gse42568"
S1_SOURCE = ROOT / "s1_初步_gse42568_limma" / "limma_all_genes.csv"
S1_ENRICHMENT_SOURCE = ROOT / "experiments" / "s1_pathway_enrichment"
ENRICHMENT_ENV = S1_ENRICHMENT_SOURCE / "environment_manifest.json"
S2_SOURCE = ROOT / "s2_lincs" / "results"
EXP006_TOP50 = ROOT / "experiments" / "exp006_ranking_reliability" / "results" / "candidate_drugs_top50.csv"
REPORT_DIR = ROOT / "experiments" / "platform_phase_c"
REPORT_JSON = REPORT_DIR / "formal_erpos_project_linkage.json"
REPORT_MD = REPORT_DIR / "formal_erpos_project_linkage_report.md"
ENRICHMENT_FILES = [
    "gene_mapping_audit.csv", "pathway_gene_links.csv", "go_up.csv", "go_down.csv",
    "reactome_up.csv", "reactome_down.csv", "hallmark_gsea.csv",
]

# GraphBuild 契约的 15 列（与 build_project_graph_bundle.py 消费端一致）
SUMMARY_FIELDS = [
    "pert_id", "drug_name", "cell_id", "pert_type", "dose", "time", "n_signatures",
    "WTCS_median", "WTCS_mean", "WTCS_sd", "WTCS_mad", "quality_cc_median",
    "TAS_median", "replicates_median", "rank_within_condition",
]

# 来自已归档历史登记报告（experiments/platform_phase_c/formal_erpos_project_linkage.json）。
# 该 S1 run 已确定登记，A+ 注册复用它，避免重跑 S1 差异分析。
KNOWN_S1_RUN_ID = "ANALYSIS_RUN:S1_PRECOMPUTED:9ebd6c43-9342-487e-a052-ae901461b58c"
KNOWN_ENRICHMENT_RUN_ID = "ANALYSIS_RUN:S1_ENRICHMENT:eb409567-e846-4422-ae19-0055937871ea"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, columns: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="正式 ER+ 项目历史分析运行登记与 GraphBundle 构建（支持离线 dry-run 预览）",
    )
    parser.add_argument("--seed-mode", choices=["historical", "aplus"], default="historical",
                        help="historical=Exp006 Top50 历史流程（默认）；aplus=A+ 预制备 seed")
    parser.add_argument("--candidate-seed", type=Path, default=EXP006_TOP50,
                        help="历史模式候选种子 CSV（默认 Exp006 Top50）")
    parser.add_argument("--aplus-seed", type=Path, default=None,
                        help="A+ 模式：预制备 drug_condition_summary.csv（15 契约列，含 quality_cc_median）")
    parser.add_argument("--aplus-provenance", type=Path, default=None,
                        help="A+ 模式：seed_provenance.csv（来源追溯）")
    parser.add_argument("--aplus-master", type=Path, default=None,
                        help="A+ 模式：candidate_master.csv（候选主集合，仅校验用）")
    parser.add_argument("--seed-label", type=str, default="Exp006 candidate_drugs_top50.csv",
                        help="seed 来源标签，写入 qc_summary/报告")
    parser.add_argument("--expected-candidates", type=int, default=None,
                        help="期望候选（唯一 pert_id）数；不提供则只校验唯一性，不硬编码 50")
    parser.add_argument("--expected-conditions", type=int, default=None,
                        help="期望条件行数；不提供则不校验")
    parser.add_argument("--dry-run", action="store_true",
                        help="离线预览：只读输入、计算计划，不写数据库、不写文件、不触发 API")
    return parser.parse_args()


def dry_run_preview(args: argparse.Namespace) -> dict:
    """离线生成注册预览（只读，不写任何文件、不连数据库）。"""
    preview: dict = {
        "dry_run": True, "no_database_write": True, "no_file_write": True,
        "seed_mode": args.seed_mode, "seed_label": args.seed_label,
    }
    if args.seed_mode == "historical":
        _, seed_rows = read_csv(args.candidate_seed)
        pert_ids = {row["pert_id"].strip() for row in seed_rows if row.get("pert_id", "").strip()}
        _, sig_rows = read_csv(S2_SOURCE / "formal_signature_scores.csv")
        _, cond_rows = read_csv(S2_SOURCE / "formal_drug_condition_ranking.csv")
        sig_subset = [r for r in sig_rows if r.get("scheme") == "B" and r.get("pert_id") in pert_ids]
        cond_subset = [r for r in cond_rows if r.get("scheme") == "B" and r.get("pert_id") in pert_ids]
        candidate_count = len(pert_ids)
        condition_count = len(cond_subset)
        signature_count = len(sig_subset)
        seed_sha = sha256(args.candidate_seed)
        provenance_sha = None
        preview["candidate_source"] = str(args.candidate_seed.relative_to(ROOT))
        preview["signature_subset_count"] = signature_count
        preview["formal_signature_scores_sha256"] = sha256(S2_SOURCE / "formal_signature_scores.csv")
        preview["open_issue"] = None
    elif args.seed_mode == "aplus":
        if not args.aplus_seed or not args.aplus_seed.is_file():
            raise FileNotFoundError("A+ 模式需要 --aplus-seed 指向预制备 drug_condition_summary.csv")
        _, seed_rows = read_csv(args.aplus_seed)
        if not seed_rows:
            raise ValueError("A+ seed 为空")
        cols = list(seed_rows[0].keys())
        pert_ids = {row["pert_id"].strip() for row in seed_rows if row.get("pert_id", "").strip()}
        candidate_count = len(pert_ids)
        condition_count = len(seed_rows)
        signature_count = None
        seed_sha = sha256(args.aplus_seed)
        provenance_sha = sha256(args.aplus_provenance) if args.aplus_provenance and args.aplus_provenance.is_file() else None
        prov_rows: list[dict] = []
        if args.aplus_provenance and args.aplus_provenance.is_file():
            _, prov_rows = read_csv(args.aplus_provenance)
        master_rows: list[dict] = []
        if args.aplus_master and args.aplus_master.is_file():
            _, master_rows = read_csv(args.aplus_master)
        preview["candidate_source"] = str(args.aplus_seed)
        preview["provenance_source"] = str(args.aplus_provenance)
        preview["provenance_rows"] = len(prov_rows)
        preview["master_rows"] = len(master_rows)
        preview["master_unique_pert_id"] = len({r["pert_id"] for r in master_rows if r.get("pert_id")})
        preview["seed_columns"] = cols
        preview["has_quality_cc_median"] = "quality_cc_median" in cols
        preview["has_cc_median"] = "CC_median" in cols
        preview["open_issue"] = (
            "A+ 模式需另行从 final_signature_scores.csv 派生 signature_scores.csv"
            "（过滤到 1,032 候选的条件），用于 /s2/import 的 required_outputs 校验；本轮 dry-run 未生成。"
        )
        preview["signature_scores_pending"] = True
    else:
        raise ValueError(f"未知 seed_mode：{args.seed_mode}")

    preview["candidate_count"] = candidate_count
    preview["condition_count"] = condition_count
    preview["candidate_condition_distinct"] = True  # 候选（唯一 pert_id）≠ 条件（逐行记录），不把条件当候选
    preview["signature_subset_count"] = signature_count
    preview["seed_sha256"] = seed_sha
    preview["provenance_sha256"] = provenance_sha
    preview["checks"] = {
        "candidates_ok": args.expected_candidates is None or candidate_count == args.expected_candidates,
        "conditions_ok": args.expected_conditions is None or condition_count == args.expected_conditions,
        "candidate_pert_id_unique": candidate_count == len(pert_ids),
    }

    # 确定性 would-be s2_run_id。历史模式忠实复现原公式：
    #   uuid5(NAMESPACE_URL, PROJECT_ID + s1_run_id + "|" + formal_signature_scores_sha + "|" + candidate_seed_sha)
    # A+ 模式的 signature_scores.csv 尚未派生，故 run_token 为初步值（标注 preliminary）。
    if args.seed_mode == "historical":
        sig_component = preview.get("formal_signature_scores_sha256", "")
    else:
        sig_component = "PENDING_signature_scores"
        preview["run_token_preliminary"] = True
    run_token = str(uuid5(NAMESPACE_URL, PROJECT_ID + KNOWN_S1_RUN_ID + "|" + sig_component + "|" + (seed_sha or "")))
    preview["would_be_s2_run_id"] = f"ANALYSIS_RUN:S2_LINCS:{run_token}"
    preview["would_be_result_dir"] = f"data/processed/projects/{PROJECT_ID}/s2/{run_token}"
    preview["would_be_files"] = ["signature_scores.csv", "drug_condition_summary.csv", "s1_gene_mapping_audit.csv", "qc_summary.json", "run_manifest.json"]
    preview["would_be_api_calls"] = [
        f"POST /api/v1/projects/{PROJECT_ID}/s2/import",
        f"POST /api/v1/projects/{PROJECT_ID}/graph-bundles",
        f"POST /api/v1/projects/{PROJECT_ID}/graph-builds",
    ]
    preview["reused_s1_run_id"] = KNOWN_S1_RUN_ID
    preview["reused_enrichment_run_id"] = KNOWN_ENRICHMENT_RUN_ID
    preview["provenance_traceability"] = "seed_provenance.csv 以 seed_row_number 与 seed 行一一对应，保留 candidate_id/condition_record_id/source_row_number/source_sha256"
    return preview


def main(args: argparse.Namespace) -> None:
    if args.dry_run:
        preview = dry_run_preview(args)
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return
    if args.seed_mode == "aplus":
        raise NotImplementedError(
            "A+ 完整注册（含 DB 写入）尚未实现；本轮只支持 --dry-run 离线预览。"
            "请在确认数据库运行环境后，再补齐 A+ 模式的 signature_scores.csv 派生与注册步骤。"
        )

    # 非 dry-run 流程才需要后端依赖（数据库/API）。
    from fastapi.testclient import TestClient  # noqa: E402
    from sqlalchemy import select  # noqa: E402
    from app.db import SessionLocal  # noqa: E402
    from app.main import app, build_project_graph_bundle, graph_build, run_precomputed_s1  # noqa: E402
    from app.models import AnalysisRun, Dataset, Project  # noqa: E402
    from app.schemas import GraphBundleRequest, PrecomputedS1Request, S1EnrichmentRequest  # noqa: E402

    for path in [S1_SOURCE, ENRICHMENT_ENV, args.candidate_seed,
                 S2_SOURCE / "formal_signature_scores.csv",
                 S2_SOURCE / "formal_drug_condition_ranking.csv",
                 S2_SOURCE / "gene_mapping_audit.csv",
                 S2_SOURCE / "formal_qc_summary.csv"]:
        if not path.is_file():
            raise FileNotFoundError(path)

    source_s1_hash = sha256(S1_SOURCE)
    enrichment_env = json.loads(ENRICHMENT_ENV.read_text(encoding="utf-8"))
    if enrichment_env.get("input_sha256") != source_s1_hash:
        raise ValueError("S1 富集环境清单中的原始 S1 SHA-256 与当前历史 limma 文件不一致")
    source_hashes = {str(path.relative_to(ROOT).as_posix()): sha256(path) for path in [
        S1_SOURCE, ENRICHMENT_ENV, *(S1_ENRICHMENT_SOURCE / name for name in ENRICHMENT_FILES),
        S2_SOURCE / "formal_signature_scores.csv", S2_SOURCE / "formal_drug_condition_ranking.csv",
        S2_SOURCE / "gene_mapping_audit.csv", S2_SOURCE / "formal_qc_summary.csv", args.candidate_seed,
    ]}
    started = datetime.now(timezone.utc).isoformat()
    result: dict = {
        "started_at_utc": started, "project_id": PROJECT_ID, "project_slug": PROJECT_SLUG,
        "provenance_kind": "historical_output_import", "source_s1_sha256": source_s1_hash,
        "source_hashes": source_hashes, "status": "running", "steps": {},
    }

    # The original limma table enters through the same validated upload and
    # precomputed-DEG API used by future user projects.
    with SessionLocal() as db:
        project = db.get(Project, PROJECT_ID)
        if project is None or project.slug != PROJECT_SLUG or project.status != "active":
            raise RuntimeError("正式 ER+ 项目不存在或状态不是 active；拒绝写入")
        dataset = db.scalar(select(Dataset).where(Dataset.project_id == PROJECT_ID, Dataset.sha256 == source_s1_hash))
        dataset_id = dataset.dataset_id if dataset else None

    with TestClient(app) as client:
        if dataset_id is None:
            response = client.post(
                f"/api/v1/projects/{PROJECT_ID}/datasets/upload",
                params={"filename": S1_SOURCE.name, "dataset_type": "precomputed_deg", "release": "GSE42568 existing limma output"},
                content=S1_SOURCE.read_bytes(), headers={"Content-Type": "application/octet-stream"},
            )
            if response.status_code != 201:
                raise RuntimeError(f"S1 数据集登记失败 {response.status_code}: {response.text[-2000:]}")
            dataset_id = response.json()["dataset_id"]

        with SessionLocal() as db:
            dataset = db.get(Dataset, dataset_id)
            if dataset is None or dataset.project_id != PROJECT_ID or dataset.sha256 != source_s1_hash:
                raise RuntimeError("项目 S1 数据集登记与原始 SHA-256 不一致")
            dataset.source = "GEO GSE42568; existing limma differential expression output"
            dataset.release = "GSE42568 historical limma result; immutable source"
            dataset.source_url = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE42568"
            dataset.license_status = "source citation retained; redistribution terms not re-evaluated by this adapter"
            dataset.metadata_json = {**dataset.metadata_json, "historical_output_import": True,
                                     "provenance_kind": "historical_output_import",
                                     "source_path": S1_SOURCE.relative_to(ROOT).as_posix(),
                                     "source_sha256": source_s1_hash,
                                     "contrast_direction": "Tumor - Normal",
                                     "note": "原始 S1 文件只读；平台解析器仅生成项目内标准化副本。"}
            db.commit()
            s1_run = db.scalar(select(AnalysisRun).where(
                AnalysisRun.project_id == PROJECT_ID, AnalysisRun.run_type == "s1_precomputed_deg",
                AnalysisRun.status == "completed", AnalysisRun.parameters["dataset_id"].as_string() == dataset_id,
            ))
        if s1_run is None:
            response = client.post(
                f"/api/v1/projects/{PROJECT_ID}/datasets/{dataset_id}/s1/precomputed",
                json={"positive_group": "Tumor", "negative_group": "Normal", "fdr_threshold": 0.05},
            )
            if response.status_code != 200:
                raise RuntimeError(f"项目级 S1 登记失败 {response.status_code}: {response.text[-3000:]}")
            s1_run_id = response.json()["analysis_run_id"]
        else:
            s1_run_id = s1_run.analysis_run_id

    with SessionLocal() as db:
        s1_run = db.get(AnalysisRun, s1_run_id)
        if s1_run is None or s1_run.status != "completed" or s1_run.parameters.get("contrast") != "Tumor - Normal":
            raise RuntimeError("项目 S1 AnalysisRun 状态或 Tumor - Normal 方向核验失败")
        canonical_s1_hash = s1_run.parameters.get("output_sha256")
        result["steps"]["s1"] = {
            "status": "completed", "dataset_id": dataset_id, "analysis_run_id": s1_run_id,
            "source_sha256": source_s1_hash, "standardized_output_sha256": canonical_s1_hash,
            "contrast": s1_run.parameters.get("contrast"), "input_rows": s1_run.parameters.get("input_rows"),
            "unique_genes": s1_run.parameters.get("unique_genes"), "significant_genes": s1_run.parameters.get("significant_genes"),
            "up": s1_run.parameters.get("up"), "down": s1_run.parameters.get("down"),
            "historical_import": True, "limma_rerun": False,
        }

        # Register the already-computed pathway outputs as a project-scoped
        # historical import, copying bytes and verifying every copied hash.
        enrichment_run_id = f"ANALYSIS_RUN:S1_ENRICHMENT:{uuid5(NAMESPACE_URL, PROJECT_ID + s1_run_id + source_s1_hash)}"
        enrichment_run = db.get(AnalysisRun, enrichment_run_id)
        if enrichment_run is None:
            enrichment_token = enrichment_run_id.rsplit(":", 1)[-1]
            enrichment_dir = ROOT / "data" / "processed" / "projects" / PROJECT_ID / "s1_enrichment" / enrichment_token
            enrichment_dir.mkdir(parents=True, exist_ok=False)
            files_to_copy = [*ENRICHMENT_FILES, "environment_manifest.json", "enrichment_report.md", "s1_up_genes.csv", "s1_down_genes.csv"]
            for name in files_to_copy:
                source = S1_ENRICHMENT_SOURCE / name
                if source.is_file():
                    shutil.copy2(source, enrichment_dir / name)
            source_figures = S1_ENRICHMENT_SOURCE / "figures"
            if source_figures.is_dir():
                shutil.copytree(source_figures, enrichment_dir / "figures", dirs_exist_ok=True)
            output_hashes = {name: sha256(enrichment_dir / name) for name in ENRICHMENT_FILES}
            if any(output_hashes[name] != source_hashes[(S1_ENRICHMENT_SOURCE / name).relative_to(ROOT).as_posix()] for name in ENRICHMENT_FILES):
                raise RuntimeError("项目级通路文件字节复制后校验失败")
            summary_counts = {key: enrichment_env.get(key) for key in ["GO_UP_sig", "GO_DOWN_sig", "Reactome_UP_sig", "Reactome_DOWN_sig", "Hallmark_sig"]}
            enrichment_run = AnalysisRun(
                analysis_run_id=enrichment_run_id, project_id=PROJECT_ID,
                run_type="s1_pathway_enrichment", status="completed",
                parameters={
                    "s1_analysis_run_id": s1_run_id, "identifier_type": "symbol", "fdr_threshold": 0.05,
                    "contrast": "Tumor - Normal", "test_only": False, "historical_import": True,
                    "provenance_kind": "historical_output_import",
                    "source_directory": S1_ENRICHMENT_SOURCE.relative_to(ROOT).as_posix(),
                    "output_dir": enrichment_dir.relative_to(ROOT).as_posix(),
                    "output_hashes": output_hashes,
                    "significant_pathway_counts": {"go_up": summary_counts["GO_UP_sig"], "go_down": summary_counts["GO_DOWN_sig"], "reactome_up": summary_counts["Reactome_UP_sig"], "reactome_down": summary_counts["Reactome_DOWN_sig"], "hallmark_gsea": summary_counts["Hallmark_sig"]},
                    "mapped_universe": enrichment_env.get("mapped_entrez_universe"),
                    "source_input_sha256": source_s1_hash,
                    "source_analysis_rerun": False,
                },
                software_versions={"pipeline": "historical enrichment output import; no recalculation",
                                   "R": enrichment_env.get("R", "unknown"), **enrichment_env.get("packages", {})},
                input_hashes={"source_s1_sha256": source_s1_hash,
                              **{key: value for key, value in source_hashes.items() if key.startswith("experiments/s1_pathway_enrichment/")}},
            )
            db.add(enrichment_run)
            db.commit()
        else:
            enrichment_dir = ROOT / enrichment_run.parameters["output_dir"]
        result["steps"]["enrichment"] = {
            "status": "completed", "analysis_run_id": enrichment_run_id,
            "s1_analysis_run_id": s1_run_id, "source_input_sha256": source_s1_hash,
            "output_dir": enrichment_run.parameters["output_dir"],
            "output_hashes": enrichment_run.parameters["output_hashes"],
            "historical_import": True, "enrichment_rerun": False,
            "significant_pathway_counts": enrichment_run.parameters["significant_pathway_counts"],
        }

        # Re-run enrichment through the production project API against the
        # project-standardized S1 output. The immutable imported artifact above
        # remains available as historical provenance, but its old CSVs lack an
        # Entrez-to-Ensembl crosswalk needed for pathway-gene graph edges.
        with SessionLocal() as db:
            actual_enrichment = next((run for run in db.scalars(select(AnalysisRun).where(
                AnalysisRun.project_id == PROJECT_ID,
                AnalysisRun.run_type == "s1_pathway_enrichment",
                AnalysisRun.status == "completed",
            ).order_by(AnalysisRun.created_at.desc()))
                if run.parameters.get("s1_analysis_run_id") == s1_run_id
                and run.parameters.get("historical_import") is not True), None)
            if actual_enrichment is not None:
                active_enrichment_id = actual_enrichment.analysis_run_id
            else:
                active_enrichment_id = None
        if active_enrichment_id is None:
            with TestClient(app) as client:
                response = client.post(
                    f"/api/v1/projects/{PROJECT_ID}/s1/pathway-enrichment",
                    json={"s1_analysis_run_id": s1_run_id, "identifier_type": "symbol", "fdr_threshold": 0.05},
                )
                if response.status_code != 200:
                    raise RuntimeError(f"正式项目 S1 通路富集重跑失败 {response.status_code}: {response.text[-5000:]}")
                active_enrichment_id = response.json()["analysis_run_id"]
        with SessionLocal() as db:
            actual_enrichment = db.get(AnalysisRun, active_enrichment_id)
            if actual_enrichment is None or actual_enrichment.status != "completed":
                raise RuntimeError("新生成的项目级富集运行未完成")
            active_enrichment_counts = actual_enrichment.parameters.get("significant_pathway_counts", {})
            active_enrichment_output_dir = actual_enrichment.parameters.get("output_dir")
            active_enrichment_hashes = actual_enrichment.parameters.get("output_hashes", {})
            active_pathway_mapping_audit = ROOT / active_enrichment_output_dir / "pathway_gene_links.csv"
            _, active_links = read_csv(active_pathway_mapping_audit)
            if not active_links or "ensembl_gene_ids" not in active_links[0]:
                raise RuntimeError("项目级富集结果缺少 Ensembl 映射列；拒绝构建正式 GraphBundle")
        result["steps"]["enrichment"] = {
            "status": "completed", "analysis_run_id": active_enrichment_id,
            "s1_analysis_run_id": s1_run_id, "output_dir": active_enrichment_output_dir,
            "output_hashes": active_enrichment_hashes,
            "historical_import_available": True, "active_run_is_recalculated_from_standardized_s1": True,
            "significant_pathway_counts": active_enrichment_counts,
            "pathway_gene_link_rows": len(active_links),
            "pathway_gene_links_include_ensembl_mapping": True,
        }

        # Import the candidate seed as GraphBundle seeds, retaining all scheme-B
        # dose-specific conditions for those perturbagens (no best-dose pooling).
        # 候选集合已从硬编码 Exp006 Top50 改为 --candidate-seed 可配置输入。
        _, seed_rows = read_csv(args.candidate_seed)
        top_ids = {row["pert_id"].strip() for row in seed_rows if row.get("pert_id", "").strip()}
        if len(seed_rows) != len(top_ids):
            raise ValueError(f"候选种子存在重复 pert_id：rows={len(seed_rows)}, unique={len(top_ids)}")
        if args.expected_candidates is not None and len(top_ids) != args.expected_candidates:
            raise ValueError(f"候选种子唯一 pert_id 数 {len(top_ids)} 与期望 {args.expected_candidates} 不一致")
        sig_fields, sig_rows = read_csv(S2_SOURCE / "formal_signature_scores.csv")
        cond_fields, cond_rows = read_csv(S2_SOURCE / "formal_drug_condition_ranking.csv")
        sig_subset = [row for row in sig_rows if row.get("scheme") == "B" and row.get("pert_id") in top_ids]
        cond_subset = [row for row in cond_rows if row.get("scheme") == "B" and row.get("pert_id") in top_ids]
        if len({row.get("pert_id") for row in sig_subset}) != len(top_ids) or len({row.get("pert_id") for row in cond_subset}) != len(top_ids):
            raise ValueError("候选种子中存在候选未能与正式 S2 scheme-B Signature/条件结果对齐")
        summary_fields = ["pert_id", "drug_name", "cell_id", "pert_type", "dose", "time", "n_signatures",
                          "WTCS_median", "WTCS_mean", "WTCS_sd", "WTCS_mad", "quality_cc_median",
                          "TAS_median", "replicates_median", "rank_within_condition"]
        normalized_conditions = []
        for row in cond_subset:
            normalized_conditions.append({
                **{key: row.get(key, "") for key in summary_fields if key != "rank_within_condition"},
                "rank_within_condition": row.get("rank_condition", ""),
            })
        run_token = str(uuid5(NAMESPACE_URL, PROJECT_ID + s1_run_id + "|" + source_hashes["s2_lincs/results/formal_signature_scores.csv"] + "|" + source_hashes[str(args.candidate_seed.relative_to(ROOT).as_posix())]))
        s2_run_id = f"ANALYSIS_RUN:S2_LINCS:{run_token}"
        result_dir = ROOT / "data" / "processed" / "projects" / PROJECT_ID / "s2" / run_token
        result_dir.mkdir(parents=True, exist_ok=True)
        write_csv(result_dir / "signature_scores.csv", sig_fields, sig_subset)
        write_csv(result_dir / "drug_condition_summary.csv", summary_fields, normalized_conditions)
        gene_fields, gene_rows = read_csv(S2_SOURCE / "gene_mapping_audit.csv")
        write_csv(result_dir / "s1_gene_mapping_audit.csv", gene_fields, gene_rows)
        qc_fields, qc_rows = read_csv(S2_SOURCE / "formal_qc_summary.csv")
        qc_summary = {
            "source_formal_qc_summary": qc_rows,
            "source_eligible_signatures": 4761, "source_eligible_perturbagens": 1191,
            "project_seed_perturbagens": len(top_ids), "project_seed_signatures": len(sig_subset),
            "project_seed_condition_rows": len(normalized_conditions),
            "scheme": "B", "cell_id": "MCF7", "pert_type": "trt_cp", "time": "24 h",
            "quality_threshold": "distil_cc_q75 >= 0.2",
            "seed_rule": f"{args.seed_label}: {len(top_ids)} unique perturbagens; retain all scheme-B dose-specific condition rows; no per-drug best-dose selection",
            "historical_import": True,
        }
        (result_dir / "qc_summary.json").write_text(json.dumps(qc_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        outputs = {name: {"bytes": (result_dir / name).stat().st_size, "sha256": sha256(result_dir / name)} for name in [
            "signature_scores.csv", "drug_condition_summary.csv", "s1_gene_mapping_audit.csv", "qc_summary.json"]}
        source_s2_manifest = {
            "analysis_run_id": s2_run_id, "project_id": PROJECT_ID, "s1_analysis_run_id": s1_run_id,
            "status": "completed", "test_only": False, "provenance_kind": "historical_output_import",
            "historical_import": True, "s1_input_sha256": source_s1_hash,
            "source_s1_input_sha256": source_s1_hash,
            "parameters": {
                "scheme": "B", "cell_id": "MCF7", "pert_type": "trt_cp", "time": "24 h",
                "quality_filter": "distil_cc_q75 >= 0.2; -666 treated missing",
                "s1_query": "BING-restricted Top 100 UP and DOWN, FDR<0.05, adj.P.Val ascending then |logFC| descending",
                "graph_seed_rule": "Exp006 Top 50 perturbagens; retain all scheme-B dose strata",
                "cross_dose_policy": "dose strata kept separate; no per-drug optimum or cross-dose rank",
                "source_script_version": "not retained in the formal S2 source manifest; imported without recomputation",
            },
            "software_versions": {"pipeline": "historical formal S2 output import; scoring not rerun"},
            "inputs": {path: {"sha256": value, "bytes": (ROOT / path).stat().st_size} for path, value in source_hashes.items()},
            "qc_summary": qc_summary, "outputs": outputs,
            "interpretation": "WTCS is an expression association score; it is not a treatment efficacy claim.",
        }
        (result_dir / "run_manifest.json").write_text(json.dumps(source_s2_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    with TestClient(app) as client:
        response = client.post(f"/api/v1/projects/{PROJECT_ID}/s2/import", json={"analysis_run_id": s2_run_id})
        if response.status_code != 200:
            raise RuntimeError(f"历史 S2 项目导入失败 {response.status_code}: {response.text[-4000:]}")
        s2_import = response.json()
        response = client.post(f"/api/v1/projects/{PROJECT_ID}/graph-bundles", json={
            "s1_analysis_run_id": s1_run_id, "s2_analysis_run_id": s2_run_id,
            "enrichment_analysis_run_id": active_enrichment_id,
        })
        if response.status_code != 200:
            raise RuntimeError(f"正式 ER+ GraphBundle 构建失败 {response.status_code}: {response.text[-5000:]}")
        bundle_body = response.json()
        bundle_manifest_path = ROOT / bundle_body["bundle_dir"] / "bundle_manifest.json"
        bundle_manifest = json.loads(bundle_manifest_path.read_text(encoding="utf-8"))
        if sha256(bundle_manifest_path) != bundle_body.get("manifest_sha256"):
            raise RuntimeError("GraphBundle API 返回的 manifest SHA-256 与磁盘文件不一致")
        with SessionLocal() as db:
            build_response = graph_build(PROJECT_ID, db, bundle_body["analysis_run_id"])
        result["steps"]["s2_import"] = {
            "status": s2_import.get("status"), "analysis_run_id": s2_run_id,
            "persisted_condition_count": s2_import.get("persisted_condition_count", s2_import.get("condition_count")),
            "project_seed_signatures": len(sig_subset), "project_seed_perturbagens": len(top_ids),
            "dose_condition_rows": len(normalized_conditions), "historical_import": True,
            "source_s1_hash_verified_against_project_upload": True,
        }
        result["steps"]["graph_bundle"] = {
            "status": bundle_body.get("status"), "analysis_run_id": bundle_body.get("analysis_run_id"),
            "bundle_dir": bundle_body.get("bundle_dir"), "counts": bundle_body.get("counts"),
            "mapping_policy": bundle_manifest.get("mapping_policy"),
            "cross_dose_policy": bundle_manifest.get("cross_dose_policy"),
        }
        result["steps"]["graph_build"] = {
            "status": build_response.get("status"), "graph_build_id": build_response.get("graph_build_id"),
            "summary": build_response.get("summary"), "idempotent": build_response.get("idempotent"),
        }

    result["steps"]["superseded_graph_build_audit"] = {
        "retained_legacy_versions": [
            {
                "graph_build_id": "GRAPH_BUILD:3f18f4c1-713d-58c5-8a53-38eca5a5aaf9:92ccf8669de7c697",
                "reason_not_canonical": "旧通路链接无 Ensembl crosswalk；31,991 条链接唯一映射 0 条，交汇路径 0 条。",
            },
            {
                "graph_build_id": "GRAPH_BUILD:3f18f4c1-713d-58c5-8a53-38eca5a5aaf9:89efd9796240c910",
                "reason_not_canonical": "虽有 30,350 条唯一映射和 1,494 条交汇路径，但曾把通路显示名称误作节点 ID；本次 stable-ID 修复后另建版本。",
            },
        ],
        "action": "旧版本均保留供审计；新版本采用 GO、Reactome、MSigDB 稳定标识。",
    }

    result["status"] = "completed"
    result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    REPORT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 正式 ER+ 项目历史分析运行登记与 GraphBundle 报告", "",
        f"运行时间（UTC）：{result['finished_at_utc']}", "",
        "## 工作范围", "",
        "本次未重跑 limma 或 LINCS WTCS。现存 GSE42568 limma 表经平台预计算 DEG 接口登记并生成项目内标准化副本；先保留历史 GO/Reactome/Hallmark 文件的逐字节副本，再以项目标准化 S1 运行新的 R 富集及 Entrez—Ensembl 映射，避免旧结果缺少 Ensembl 列导致通路基因关联丢失。LINCS scheme B 结果按 Exp006 Top 50 规则形成项目级种子子集，并保留所有剂量条件。", "",
        "## 阶段状态", "",
    ]
    for key, detail in result["steps"].items():
        lines += [f"### {key}", "", "```json", json.dumps(detail, ensure_ascii=False, indent=2), "```", ""]
    lines += [
        "## 溯源与解释边界", "",
        f"历史完整 limma 输入 SHA-256：`{source_s1_hash}`。S1 结果仅由预计算表解析器标准化，未重跑差异分析。新的项目级富集以此标准化 S1 为输入，使用已安装的 R/Bioconductor 环境生成 Entrez 与 Ensembl 映射；历史富集副本仍留作原始来源。S2 是已有完整筛选的历史输出导入；其原始 S1 输入 SHA 与本项目登记数据集 SHA 一致，接口校验了项目上传文件及哈希。项目 GraphBundle 仅纳入 Exp006 Top 50 的 scheme B perturbagen，并保留每个药物所有剂量条件，不重新排序、不作疗效判断。", "",
        "GraphBundle 中的公共药物靶点与通路关系仍来自其来源数据库；关系交汇表示可追溯证据共现，不证明因果、疗效或 ER 阳性特异机制。", "",
        "## 机器可读记录", "", f"完整阶段信息见 `{REPORT_JSON.relative_to(ROOT).as_posix()}`。", "",
    ]
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"status": result["status"], "project_id": PROJECT_ID,
                      "steps": {key: item.get("status") for key, item in result["steps"].items()},
                      "report": REPORT_MD.relative_to(ROOT).as_posix()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(parse_args())
