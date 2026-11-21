"""在两个独立项目之间验证 Neo4j 公共实体复用与 GraphBuild 隔离。

第二个项目及其 Candidate/S1/S2 关系都是显式 test_only 软件夹具，
不表示真实药物实验、候选排名或疗效结论。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT / "backend")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AnalysisRun, GraphBuild, Project  # noqa: E402
from kg.scripts.import_phase_b_to_neo4j import main as import_bundle  # noqa: E402


SOURCE_PROJECT_ID = "89aa18d8-e1ca-4eb4-8046-634cd2911207"
SOURCE_BUILD_ID = f"GRAPH_BUILD:{SOURCE_PROJECT_ID}:52b0590ca225433b"
SOURCE_BUNDLE_ID = "abcd527b-66c5-4b16-b5a9-4d9526f260ce"
PROJECT_NAMESPACE = UUID("7dc22d76-272a-4e28-86c4-802dca026706")
REPORT_JSON = ROOT / "experiments" / "platform_phase_c" / "phase_e_neo4j_multi_project_validation.json"
REPORT_MD = ROOT / "experiments" / "platform_phase_c" / "phase_e_neo4j_multi_project_report.md"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def stable_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def edge(source: str, label: str, target: str, relation: str, release: str, props: dict) -> dict:
    return {
        "from": source,
        "label": label,
        "to": target,
        "relation": relation,
        "undirected": "false",
        "source_release": release,
        "properties_json": stable_json(props),
    }


def main() -> dict:
    if settings.graph_source != "neo4j":
        raise RuntimeError("需要 GRAPH_SOURCE=neo4j 才能执行数据库级验收")
    if not settings.database_url.startswith("postgresql"):
        raise RuntimeError("需要 PostgreSQL 作为项目与 GraphBuild 登记库")

    source_bundle = ROOT / "data" / "processed" / "projects" / SOURCE_PROJECT_ID / "graph_bundle" / "runs" / SOURCE_BUNDLE_ID
    if not (source_bundle / "subgraph_nodes.csv").is_file() or not (source_bundle / "subgraph_edges.csv").is_file():
        raise FileNotFoundError(f"已验收的第一项目 GraphBundle 不存在：{source_bundle}")

    route_rows = read_csv(source_bundle / "candidate_target_pathway_routes.csv")
    nodes = {row["id"]: row for row in read_csv(source_bundle / "subgraph_nodes.csv")}
    all_edges = read_csv(source_bundle / "subgraph_edges.csv")
    chosen = None
    for target_edge in all_edges:
        if target_edge["label"] != "DRG-GEN":
            continue
        public_drug, gene = target_edge["from"], target_edge["to"]
        pathway_edge = next((row for row in all_edges if row["label"] == "S1-ENRICHED-GENE" and row["to"] == gene and row["from"].startswith(("GO:", "REACT:", "MSIGDB:"))), None)
        route = next((row for row in route_rows if row["drug_id"] == public_drug and row["target_gene_id"] == gene), None)
        if pathway_edge and route:
            pathway = pathway_edge["from"]
            if all(node_id in nodes for node_id in [public_drug, gene, pathway]):
                chosen = {"drug_id": public_drug, "gene_id": gene, "pathway_id": pathway, "pert_id": route["pert_id"], "target_edge": target_edge}
                break
    if chosen is None:
        raise RuntimeError("第一项目 GraphBundle 中没有可复用的 Drug→Gene→Pathway 公共关系")

    project_id = str(uuid5(PROJECT_NAMESPACE, "phase-e-neo4j-second-project"))
    slug = "phase-e-neo4j-second-project"
    analysis_ids = {
        "s1": "ANALYSIS_RUN:S1:PHASEE_NEO4J_SECOND_PROJECT",
        "s2": "ANALYSIS_RUN:S2:PHASEE_NEO4J_SECOND_PROJECT",
        "enrichment": "ANALYSIS_RUN:S1_ENRICHMENT:PHASEE_NEO4J_SECOND_PROJECT",
        "bundle": "ANALYSIS_RUN:KG_GRAPH_BUNDLE:PHASEE_NEO4J_SECOND_PROJECT",
    }
    candidate_id = f"CANDIDATE:{project_id}:SOFTWARE_FIXTURE_ONLY"
    context_id = f"DISEASE_CONTEXT:{project_id}:SOFTWARE_FIXTURE_ONLY"
    condition_id = f"S2_CONDITION:{project_id}:SOFTWARE_FIXTURE_ONLY"
    sentinel = f"TEST_ONLY_SENTINEL_{project_id[:8]}"
    test_pert_id = f"TEST_ONLY_PERT:{project_id}"
    bundle_dir = ROOT / "data" / "processed" / "projects" / project_id / "graph_bundle" / "runs" / "phasee-multi-project"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    selected_nodes = [nodes[chosen["drug_id"]], nodes[chosen["gene_id"]], nodes[chosen["pathway_id"]]]
    test_nodes = [
        {"id": context_id, "type": "DiseaseContext", "label": "第二项目软件隔离夹具", "properties_json": stable_json({"project_id": project_id, "project_slug": slug, "disease_id": "MONDO:0008903", "analysis_run_id": analysis_ids["bundle"], "s1_analysis_run_id": analysis_ids["s1"], "s2_analysis_run_id": analysis_ids["s2"], "test_only": True, "fixture_kind": "software_validation_only"})},
        {"id": candidate_id, "type": "Candidate", "label": sentinel, "properties_json": stable_json({"project_id": project_id, "project_slug": slug, "pert_id": test_pert_id, "test_only": True, "fixture_kind": "software_validation_only", "interpretation": "不是真实 LINCS 候选药物"})},
        {"id": condition_id, "type": "S2Condition", "label": "软件验证条件（非实验数据）", "properties_json": stable_json({"project_id": project_id, "analysis_run_id": analysis_ids["s2"], "pert_id": test_pert_id, "cell_id": "TEST_ONLY", "pert_type": "test_fixture", "test_only": True})},
    ]
    node_rows = selected_nodes + test_nodes
    for row in node_rows:
        row.setdefault("properties_json", "{}")

    # 复用第一项目 GraphBundle 中真实的 OptimusKG 公开关系；新增的候选、疾病上下文、S1/S2
    # 关系均是标有 test_only 的软件夹具，只用于检验跨项目隔离。
    public_edges = [chosen["target_edge"]]
    test_release = "DiseaseGraph software test fixture; not scientific evidence"
    fixture_edges = [
        edge(context_id, "PROJECT-HAS_CANDIDATE", candidate_id, "HAS_CANDIDATE", test_release, {"test_only": True, "analysis_run_id": analysis_ids["s2"]}),
        edge(candidate_id, "PROJECT-REFERENCES_DRUG", chosen["drug_id"], "REFERENCES_DRUG", test_release, {"test_only": True, "analysis_run_id": analysis_ids["s2"], "mapping_method": "fixture-only public entity link"}),
        edge(context_id, "PROJECT-HAS_S2_CONDITION", condition_id, "HAS_S2_CONDITION", test_release, {"test_only": True, "analysis_run_id": analysis_ids["s2"]}),
        edge(context_id, "S1-ENRICHED-PATHWAY", chosen["pathway_id"], "HAS_ENRICHED_PATHWAY", test_release, {"test_only": True, "analysis_run_id": analysis_ids["enrichment"], "s1_analysis_run_id": analysis_ids["s1"]}),
        edge(chosen["pathway_id"], "S1-ENRICHED-GENE", chosen["gene_id"], "HAS_ENRICHED_GENE", test_release, {"test_only": True, "analysis_assertions": [{"analysis_run_id": analysis_ids["enrichment"], "s1_analysis_run_id": analysis_ids["s1"], "analysis": "SOFTWARE_FIXTURE", "source_record_id": f"{analysis_ids['enrichment']}:software-fixture"}]}),
        edge(context_id, "PROJECT-HAS_DEG", chosen["gene_id"], "HAS_DEG", test_release, {"test_only": True, "analysis_run_id": analysis_ids["s1"], "direction": "TEST_FIXTURE"}),
    ]
    edge_rows = public_edges + fixture_edges
    write_csv(bundle_dir / "subgraph_nodes.csv", node_rows, ["id", "type", "label", "properties_json"])
    write_csv(bundle_dir / "subgraph_edges.csv", edge_rows, ["from", "label", "to", "relation", "undirected", "source_release", "properties_json"])
    output_hashes = {name: hashlib.sha256((bundle_dir / name).read_bytes()).hexdigest() for name in ["subgraph_nodes.csv", "subgraph_edges.csv"]}
    build_key = hashlib.sha256(json.dumps({"project_id": project_id, "output_hashes": output_hashes}, sort_keys=True).encode()).hexdigest()
    build_id = f"GRAPH_BUILD:{project_id}:{build_key[:16]}"
    fixture_manifest = {"status": "validated", "project_id": project_id, "test_only": True, "fixture_kind": "software_validation_only", "source_graph_build_id": SOURCE_BUILD_ID, "analysis_run_ids": analysis_ids, "reused_public_ids": [chosen["drug_id"], chosen["gene_id"], chosen["pathway_id"]], "output_hashes": output_hashes}
    (bundle_dir / "bundle_manifest.json").write_text(json.dumps(fixture_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    db = SessionLocal()
    try:
        source_project = db.get(Project, SOURCE_PROJECT_ID)
        source_build = db.get(GraphBuild, SOURCE_BUILD_ID)
        if source_project is None or source_project.status != "test_only" or source_build is None or source_build.status != "materialized":
            raise RuntimeError("第一项目的 test_only GraphBuild 未在 PostgreSQL 中登记为 materialized")
        existing_project = db.get(Project, project_id)
        if existing_project is None:
            db.add(Project(project_id=project_id, slug=slug, name="Phase E 多项目数据库隔离测试（软件夹具）", disease_id="MONDO:0008903", status="test_only"))
            db.flush()
        elif existing_project.status != "test_only" or existing_project.slug != slug:
            raise RuntimeError("确定性测试项目 ID 已被非 test_only 数据占用")
        for role, run_type in [("s1", "s1_precomputed_deg"), ("s2", "s2_lincs"), ("enrichment", "s1_pathway_enrichment"), ("bundle", "kg_graph_bundle")]:
            run_id = analysis_ids[role]
            run = db.get(AnalysisRun, run_id)
            params = {"test_only": True, "fixture_kind": "software_validation_only", "bundle_dir": bundle_dir.relative_to(ROOT).as_posix()}
            run_status = "completed" if role == "bundle" else "fixture_only"
            if role == "bundle":
                params.update({"s1_analysis_run_id": analysis_ids["s1"], "s2_analysis_run_id": analysis_ids["s2"], "enrichment_analysis_run_id": analysis_ids["enrichment"], "output_hashes": output_hashes})
            if run is None:
                db.add(AnalysisRun(analysis_run_id=run_id, project_id=project_id, run_type=run_type, status=run_status, parameters=params, software_versions={"pipeline": "phase_e_multi_project_neo4j_fixture"}, input_hashes=output_hashes))
            elif run.project_id != project_id or run.run_type != run_type:
                raise RuntimeError(f"分析运行 ID 由其他项目或类型占用：{run_id}")
            else:
                run.status = run_status
                run.parameters = params
        build = db.get(GraphBuild, build_id)
        if build is None:
            build = GraphBuild(graph_build_id=build_id, project_id=project_id, analysis_run_id=analysis_ids["bundle"], build_key=build_key, status="importing", graph_source="neo4j", input_hashes=output_hashes, summary_json={"test_only": True, "fixture_kind": "software_validation_only"})
            db.add(build)
        elif build.project_id != project_id or build.build_key != build_key:
            raise RuntimeError("GraphBuild ID 与当前 test_only 输入不一致")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    # pydantic-settings 从 .env 读取配置，但 importer 使用 os.environ；仅在子进程内传递，不输出口令。
    os.environ.update({"NEO4J_URI": settings.neo4j_uri, "NEO4J_USER": settings.neo4j_user, "NEO4J_PASSWORD": settings.neo4j_password, "NEO4J_DATABASE": settings.neo4j_database})
    import_bundle(
        build_id,
        bundle_dir,
        None,
        project_id=project_id,
        build_key=build_key,
        expected_input_hashes_json=json.dumps(output_hashes, sort_keys=True),
    )

    db = SessionLocal()
    try:
        build = db.get(GraphBuild, build_id)
        build.status = "materialized"
        build.summary_json = {"test_only": True, "fixture_kind": "software_validation_only", "neo4j_membership": "registered", "import_status": "PASS", "shared_public_ids": [chosen["drug_id"], chosen["gene_id"], chosen["pathway_id"]], "node_count": len(node_rows), "edge_count": len(edge_rows)}
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        prefix_a = f"/api/v1/projects/{SOURCE_PROJECT_ID}"
        prefix_b = f"/api/v1/projects/{project_id}"
        summary_a = client.get(f"{prefix_a}/graph/summary", params={"graph_build_id": SOURCE_BUILD_ID})
        summary_b = client.get(f"{prefix_b}/graph/summary", params={"graph_build_id": build_id})
        assert summary_a.status_code == summary_b.status_code == 200, (summary_a.text, summary_b.text)
        summary_a_body, summary_b_body = summary_a.json(), summary_b.json()
        assert summary_a_body["node_count"] > 0 and summary_b_body["node_count"] >= 5

        s1_summary_b = client.get(f"{prefix_b}/s1/summary")
        s2_summary_b = client.get(f"{prefix_b}/s2/summary")
        assert s1_summary_b.status_code == s2_summary_b.status_code == 200
        assert not s1_summary_b.json()["available"] and not s2_summary_b.json()["available"]

        search_b = client.get(f"{prefix_b}/graph/search", params={"q": sentinel, "limit": 10, "graph_build_id": build_id})
        search_a_for_b = client.get(f"{prefix_a}/graph/search", params={"q": sentinel, "limit": 10, "graph_build_id": SOURCE_BUILD_ID})
        assert search_b.status_code == search_a_for_b.status_code == 200
        assert any(item["id"] == candidate_id for item in search_b.json()["items"])
        assert not search_a_for_b.json()["items"]

        subgraph_b = client.get(f"{prefix_b}/graph/subgraph", params={"node_id": context_id, "max_depth": 2, "limit": 50, "graph_build_id": build_id})
        assert subgraph_b.status_code == 200
        subgraph_b_ids = {item["id"] for item in subgraph_b.json()["nodes"]}
        assert {context_id, candidate_id, condition_id, chosen["drug_id"], chosen["gene_id"], chosen["pathway_id"]} <= subgraph_b_ids
        assert not any(node_id.startswith(f"CANDIDATE:{SOURCE_PROJECT_ID}:") for node_id in subgraph_b_ids)

        mechanism_b = client.get(f"{prefix_b}/drugs/{test_pert_id}/mechanisms", params={"graph_build_id": build_id})
        mechanism_a_for_b = client.get(f"{prefix_a}/drugs/{test_pert_id}/mechanisms", params={"graph_build_id": SOURCE_BUILD_ID})
        assert mechanism_b.status_code == mechanism_a_for_b.status_code == 200
        assert mechanism_b.json()["routes"] and not mechanism_a_for_b.json()["routes"]

        cross_a = client.get(f"{prefix_a}/graph/summary", params={"graph_build_id": build_id})
        cross_b = client.get(f"{prefix_b}/graph/summary", params={"graph_build_id": SOURCE_BUILD_ID})
        assert cross_a.status_code == cross_b.status_code == 404

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    try:
        with driver.session(database=settings.neo4j_database) as session:
            shared = session.run("MATCH (n) WHERE n.id IN $ids RETURN n.id AS id, count(n) AS copies, collect(DISTINCT labels(n)) AS labels", ids=[chosen["drug_id"], chosen["gene_id"], chosen["pathway_id"]]).data()
    finally:
        driver.close()
    shared_counts = {row["id"]: row["copies"] for row in shared}
    assert shared_counts == {chosen["drug_id"]: 1, chosen["gene_id"]: 1, chosen["pathway_id"]: 1}, shared_counts
    db = SessionLocal()
    try:
        analysis_statuses = {role: db.get(AnalysisRun, run_id).status for role, run_id in analysis_ids.items()}
    finally:
        db.close()
    assert analysis_statuses == {"s1": "fixture_only", "s2": "fixture_only", "enrichment": "fixture_only", "bundle": "completed"}, analysis_statuses

    result = {
        "status": "PASS",
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Neo4j 与 PostgreSQL 中的数据库级多项目软件验收；非科研结果",
        "source_project": {"project_id": SOURCE_PROJECT_ID, "slug": "software-test-graph-isolation", "status": "test_only", "graph_build_id": SOURCE_BUILD_ID, "graph_summary": {key: summary_a_body.get(key) for key in ["node_count", "edge_count", "source"]}},
        "second_project": {"project_id": project_id, "slug": slug, "status": "test_only", "graph_build_id": build_id, "analysis_run_ids": analysis_ids, "analysis_run_statuses": analysis_statuses, "fixture_nodes": len(node_rows), "fixture_edges": len(edge_rows), "graph_summary": {key: summary_b_body.get(key) for key in ["node_count", "edge_count", "source"]}},
        "reused_public_entities": [{"id": row["id"], "copies_in_neo4j": row["copies"], "labels": row["labels"]} for row in shared],
        "api_checks": {"both_graph_summaries_200": True, "second_project_s1_and_s2_results_not_fabricated": {"s1_summary_available": s1_summary_b.json()["available"], "s2_summary_available": s2_summary_b.json()["available"]}, "second_project_sentinel_only_visible_in_second_project": True, "second_project_subgraph_contains_only_its_project_context": True, "second_project_mechanism_has_test_fixture_route_only_in_second_project": True, "cross_project_graph_build_requests_return_404": [cross_a.status_code, cross_b.status_code]},
        "limitations": ["第二项目的 Candidate、S1/S2 运行关联和项目边均为 test_only 软件夹具，不是实际实验数据。", "结果验证项目/GraphBuild 隔离和公共实体 ID 复用，不证明多疾病科研结论。"],
    }
    shared_entity_text = ", ".join(
        "`{}`（Neo4j 中 {} 份）".format(row["id"], row["copies"])
        for row in shared
    )
    REPORT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_MD.write_text(
        "\n".join([
            "# Phase E：Neo4j 双项目数据库隔离验收报告", "",
            f"运行时间（UTC）：{result['run_at_utc']}", "",
            "## 验收范围", "",
            "在实际 PostgreSQL 与 Neo4j 服务中，同时保留已有 GSE42568/LINCS `test_only` GraphBuild，并新建第二个 `test_only` 软件验证项目。第二项目复用第一项目图包中的公共药物、基因、通路 ID 与公开关系；新增 Candidate、S1/S2 关联、DiseaseContext 和 S2Condition 均明确标记为软件夹具，不作为科研数据。",
            "", "## 实际结果", "",
            f"- 第一项目：`{SOURCE_PROJECT_ID}`，GraphBuild `{SOURCE_BUILD_ID}`，Neo4j 查询节点/关系数为 {summary_a_body.get('node_count')}/{summary_a_body.get('edge_count')}。",
            f"- 第二项目：`{project_id}`，状态 `test_only`，GraphBuild `{build_id}`，该测试图包有 {len(node_rows)} 个节点、{len(edge_rows)} 条边；API 返回 {summary_b_body.get('node_count')} 个节点、{summary_b_body.get('edge_count')} 条关系。",
            "- 第二项目没有伪造 S1/S2 结果：S1、S2、富集占位运行状态均为 `fixture_only`，API 的 S1/S2 summary 均返回 `available=false`；仅 GraphBundle 软件夹具运行状态为 `completed`。",
            f"- 两个项目共同复用的公共实体：{shared_entity_text}。",
            "- 两个 GraphBuild 的项目图摘要和 FastAPI 搜索/子图/机制接口均返回 200；第二项目专用哨兵药物只在第二项目可见，第二项目的测试 Perturbagen 在第一项目机制查询中无结果。",
            "- 把对方的 GraphBuild ID 传给项目接口，两侧均返回 404。",
            "", "## 验收状态", "",
            "| 检查项 | 状态 |", "|---|---|",
            "| 两个项目同时存在于 PostgreSQL 和 Neo4j | 通过 |",
            "| 公共 Drug/Gene/Pathway 按稳定 ID 复用，数据库各仅一份 | 通过 |",
            "| 项目 Candidate、DiseaseContext、S1/S2 运行和条件关系隔离 | 通过 |",
            "| 软件夹具不冒充已完成的 S1/S2 结果 | 通过 |",
            "| FastAPI 子图、搜索、机制查询及 GraphBuild 项目归属限制 | 通过 |",
            "| 软件夹具与科研结果边界清楚 | 通过 |",
            "", "## 解释边界", "",
            "第二项目新增候选链接、S1/S2 运行 ID 和项目边仅为 test_only 软件夹具；本验收证明数据库隔离和公共节点复用，不表示真实药物扰动实验、候选药物优劣或治疗效果。第一项目真实数据测试也仍不是全量 S2 筛选。",
            "", "## 机器可读记录", "",
            "完整运行 ID、GraphBuild、实际 API 状态码及复用实体见同目录 `phase_e_neo4j_multi_project_validation.json`。", "",
        ]), encoding="utf-8")
    return result


if __name__ == "__main__":
    outcome = main()
    print(json.dumps({"status": outcome["status"], "report": REPORT_MD.relative_to(ROOT).as_posix(), "second_project_id": outcome["second_project"]["project_id"], "graph_build_id": outcome["second_project"]["graph_build_id"]}, ensure_ascii=False, indent=2))
