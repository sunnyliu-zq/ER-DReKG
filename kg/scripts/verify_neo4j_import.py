from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from neo4j import GraphDatabase

try:
    from .import_phase_b_to_neo4j import canonical_properties
    from .import_phase_b_to_neo4j import expected_node_map
    from .import_phase_b_to_neo4j import expected_relationship_map
    from .import_phase_b_to_neo4j import input_fingerprint
    from .import_phase_b_to_neo4j import prepare_biocypher_import_rows
    from .import_phase_b_to_neo4j import read_graph_inputs
except ImportError:  # direct CLI execution
    from import_phase_b_to_neo4j import canonical_properties
    from import_phase_b_to_neo4j import expected_node_map
    from import_phase_b_to_neo4j import expected_relationship_map
    from import_phase_b_to_neo4j import input_fingerprint
    from import_phase_b_to_neo4j import prepare_biocypher_import_rows
    from import_phase_b_to_neo4j import read_graph_inputs

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "experiments" / "platform_phase_b"
EXT = ROOT / "experiments" / "platform_phase_b_pathways"
REPORT = EXT / "neo4j_import_validation.json"
IMPORTER = ROOT / "kg" / "scripts" / "import_phase_b_to_neo4j.py"
RELATION_MAP = {
    "DRG-GEN": "TARGETS",
    "PWY-GEN": "CONTAINS_GENE",
    "PROJECT-HAS_CANDIDATE": "HAS_CANDIDATE",
    "PROJECT-REFERENCES_DRUG": "REFERENCES_DRUG",
    "PROJECT-HAS_DEG": "HAS_DEG",
    "S1-ENRICHED-PATHWAY": "HAS_ENRICHED_PATHWAY",
    "S1-ENRICHED-GENE": "HAS_ENRICHED_GENE",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_env() -> dict[str, str]:
    values = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key and not key.startswith("#"):
                values[key] = value
    return values


def derive_graph_build_id(input_hashes: dict[str, str]) -> str:
    """Build ID is derived from the exact input bytes, so changed inputs cannot reuse it."""
    digest = hashlib.sha256(json.dumps(sorted(input_hashes.items()), ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    return f"GRAPH_BUILD:VERIFY_PHASE_B:{digest}"


def derive_build_key(project_id: str, input_hashes: dict[str, str]) -> str:
    payload = {"project_id": project_id, "input_hashes": input_hashes}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def csv_expected_counts() -> tuple[dict[str, int], dict[str, int], int, int]:
    node_types: dict[str, str] = {}
    edge_ids: dict[str, str] = {}
    edge_properties: dict[str, str] = {}
    endpoint_ids: set[str] = set()
    for path in [BASE / "subgraph_nodes.csv", EXT / "pathway_nodes.csv", EXT / "gene_nodes.csv"]:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                previous = node_types.setdefault(row["id"], row["type"])
                if previous != row["type"]:
                    raise ValueError(f"Node ID {row['id']} has conflicting types {previous}/{row['type']}")
    for path in [BASE / "subgraph_edges.csv", EXT / "pathway_edges.csv"]:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                relation = RELATION_MAP.get(row["label"])
                if not relation:
                    raise ValueError(f"Unmapped relation label {row['label']}")
                key = f"{row['from']}|{relation}|{row['to']}|{row['source_release']}"
                properties = row["properties_json"] or "{}"
                prior = edge_properties.setdefault(key, properties)
                if prior != properties:
                    raise ValueError(f"Duplicate relationship key has conflicting source assertions: {key}")
                edge_ids[key] = relation
                endpoint_ids.update([row["from"], row["to"]])
    expected_node_counts = Counter(node_types[node_id] for node_id in endpoint_ids)
    isolated_node_count = len(set(node_types) - endpoint_ids)
    return dict(expected_node_counts), dict(Counter(edge_ids.values())), len(node_types), isolated_node_count


def database_counts(driver, database: str, graph_build_id: str) -> dict:
    with driver.session(database=database) as session:
        node_rows = session.run(
            "MATCH (n) WHERE EXISTS { MATCH (n)-[r]-() WHERE $build_id IN coalesce(r.graph_build_ids, []) } "
            "UNWIND labels(n) AS label RETURN label, count(DISTINCT n) AS count ORDER BY label",
            build_id=graph_build_id,
        ).data()
        edge_rows = session.run(
            "MATCH ()-[r]->() WHERE $build_id IN coalesce(r.graph_build_ids, []) "
            "RETURN type(r) AS type, count(*) AS count ORDER BY type",
            build_id=graph_build_id,
        ).data()
        broken_evidence = session.run(
            "MATCH ()-[r]->() WHERE $build_id IN coalesce(r.graph_build_ids, []) "
            "AND (r.source_release IS NULL OR r.source_assertion_json IS NULL OR r.evidence_ids IS NULL OR size(r.evidence_ids) = 0) "
            "RETURN count(r) AS count",
            build_id=graph_build_id,
        ).single()["count"]
        unversioned_members = session.run(
            "MATCH ()-[r]->() WHERE $build_id IN coalesce(r.graph_build_ids, []) "
            "AND (r.id IS NULL OR NOT r.id ENDS WITH $suffix) RETURN count(r) AS count",
            build_id=graph_build_id,
            suffix=f"|{graph_build_id}",
        ).single()["count"]
        route = session.run(
            "MATCH (ctx:DiseaseContext {project_slug: 'erpos-gse42568'})-[hp:HAS_ENRICHED_PATHWAY]->(p) "
            "-[he:HAS_ENRICHED_GENE]->(g)<-[t:TARGETS]-(d)<-[cr:REFERENCES_DRUG]-(c) "
            "WHERE $build_id IN coalesce(hp.graph_build_ids, []) "
            "AND $build_id IN coalesce(he.graph_build_ids, []) "
            "AND $build_id IN coalesce(t.graph_build_ids, []) "
            "AND $build_id IN coalesce(cr.graph_build_ids, []) "
            "RETURN count(*) AS route_count, count(DISTINCT c.id) AS candidate_count, "
            "count(DISTINCT d.id) AS drug_count, count(DISTINCT g.id) AS target_gene_count",
            build_id=graph_build_id,
        ).single().data()
    return {
        "nodes_by_type": {row["label"]: row["count"] for row in node_rows},
        "edges_by_type": {row["type"]: row["count"] for row in edge_rows},
        "source_assertion_missing_edge_count": broken_evidence,
        "unversioned_build_member_count": unversioned_members,
        "candidate_target_pathway_routes": route,
    }


def database_inventory(driver, database: str, graph_build_id: str, node_ids: list[str]) -> dict:
    """Read the exact persisted node/relationship inventory for one GraphBuild."""
    with driver.session(database=database) as session:
        node_rows = session.run(
            "MATCH (n) WHERE n.id IN $ids "
            "RETURN n.id AS id, labels(n) AS labels, properties(n) AS properties",
            ids=node_ids,
        ).data() if node_ids else []
        relationship_rows = session.run(
            "MATCH ()-[r]->() WHERE $build_id IN coalesce(r.graph_build_ids, []) "
            "RETURN r.id AS id, type(r) AS type, properties(r) AS properties",
            build_id=graph_build_id,
        ).data()
    return {
        "nodes": {row["id"]: {"labels": row["labels"], "properties": row["properties"]} for row in node_rows},
        "relationships": {row["id"]: {"type": row["type"], "properties": row["properties"]} for row in relationship_rows},
    }


def compare_graph_inventory(expected_nodes: dict[str, dict], expected_relationships: dict[str, dict], actual: dict) -> list[str]:
    """Exact ID + property comparison, not just relationship type counts."""
    problems: list[str] = []
    actual_nodes = actual.get("nodes", {})
    for node_id, expected in expected_nodes.items():
        found = actual_nodes.get(node_id)
        if found is None:
            problems.append(f"node missing: {node_id}")
            continue
        if set(found.get("labels") or []) != {expected["label"]}:
            problems.append(f"node label drift: {node_id}")
        if canonical_properties(found.get("properties") or {}) != canonical_properties(expected["properties"]):
            problems.append(f"node property drift: {node_id}")

    actual_relationships = actual.get("relationships", {})
    for relationship_id in sorted(set(expected_relationships) - set(actual_relationships)):
        problems.append(f"relationship missing: {relationship_id}")
    for relationship_id in sorted(set(actual_relationships) - set(expected_relationships)):
        problems.append(f"relationship extra: {relationship_id}")
    for relationship_id in sorted(set(expected_relationships) & set(actual_relationships)):
        expected = expected_relationships[relationship_id]
        found = actual_relationships[relationship_id]
        if found.get("type") != expected["type"]:
            problems.append(f"relationship type drift: {relationship_id}")
        if canonical_properties(found.get("properties") or {}) != canonical_properties(expected["properties"]):
            problems.append(f"relationship property drift: {relationship_id}")
    return problems


def run_importer(env: dict[str, str], graph_build_id: str, project_id: str, build_key: str, input_hashes: dict[str, str]) -> dict:
    result = subprocess.run(
        [
            sys.executable, str(IMPORTER),
            "--graph-build-id", graph_build_id,
            "--project-id", project_id,
            "--build-key", build_key,
            "--expected-input-hashes-json", json.dumps(input_hashes, sort_keys=True, separators=(",", ":")),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode:
        raise RuntimeError(f"Importer failed (exit {result.returncode}): {result.stderr[-2000:]}")
    return json.loads(result.stdout)


def main(graph_build_id: str | None = None, project_id: str = "PHASE_B_VERIFICATION") -> None:
    env = os.environ.copy()
    env.update(load_env())
    uri = env["NEO4J_URI"]
    database = env.get("NEO4J_DATABASE", "neo4j")

    node_rows, edge_rows, input_hashes = read_graph_inputs(BASE, EXT)
    input_derived_build_id = derive_graph_build_id(input_hashes)
    if graph_build_id is not None and graph_build_id != input_derived_build_id:
        raise ValueError(
            "Explicit --graph-build-id does not match the current input hashes; "
            "omit it so different inputs cannot be mixed into one verification build"
        )
    graph_build_id = input_derived_build_id
    derived_build_id = True
    input_sha256 = input_fingerprint(input_hashes)
    build_key = derive_build_key(project_id, input_hashes)
    nodes_by_label, edges_by_relation, _ = prepare_biocypher_import_rows(
        node_rows, edge_rows, graph_build_id, build_key, input_sha256
    )
    expected_nodes = expected_node_map(nodes_by_label)
    expected_relationships = expected_relationship_map(edges_by_relation, graph_build_id)

    expected_node_counts, expected_edge_counts, unique_node_count, isolated_node_count = csv_expected_counts()
    input_paths = [BASE / "subgraph_nodes.csv", BASE / "subgraph_edges.csv", EXT / "pathway_nodes.csv", EXT / "gene_nodes.csv", EXT / "pathway_edges.csv"]
    driver = GraphDatabase.driver(uri, auth=(env["NEO4J_USER"], env["NEO4J_PASSWORD"]))
    try:
        driver.verify_connectivity()
        before = database_counts(driver, database, graph_build_id)
        import_one = run_importer(env, graph_build_id, project_id, build_key, input_hashes)
        after_first_import = database_counts(driver, database, graph_build_id)
        import_two = run_importer(env, graph_build_id, project_id, build_key, input_hashes)
        after = database_counts(driver, database, graph_build_id)
        inventory = database_inventory(driver, database, graph_build_id, sorted(expected_nodes))
    finally:
        driver.close()

    inventory_problems = compare_graph_inventory(expected_nodes, expected_relationships, inventory)
    expected = {"nodes_by_type": expected_node_counts, "edges_by_type": expected_edge_counts}
    actual = {"nodes_by_type": after["nodes_by_type"], "edges_by_type": after["edges_by_type"]}
    passed = (
        after_first_import == after
        and expected["nodes_by_type"] == actual["nodes_by_type"]
        and expected["edges_by_type"] == actual["edges_by_type"]
        and after["source_assertion_missing_edge_count"] == 0
        and after["unversioned_build_member_count"] == 0
        and isolated_node_count == 0
        and not inventory_problems
        and after["candidate_target_pathway_routes"] == {"route_count": 1422, "candidate_count": 22, "drug_count": 22, "target_gene_count": 41}
    )
    report = {
        "status": "PASS" if passed else "FAIL",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "graph_build_id": graph_build_id,
        "graph_build_id_derived_from_inputs": derived_build_id,
        "project_id": project_id,
        "analysis_run_id": "ANALYSIS_RUN:S1_PATHWAY_ENRICHMENT_20261112",
        "database": "Neo4j local Compose service",
        "count_scope": "relationships whose graph_build_ids contains graph_build_id; exact node/relationship inventory and properties",
        "import_runs": [import_one.get("status"), import_two.get("status")],
        "idempotency": {"status": "PASS" if after_first_import == after else "FAIL", "before": after_first_import, "after": after, "pre_refresh_state": before},
        "expected_unique_node_count": unique_node_count,
        "expected_isolated_node_count": isolated_node_count,
        "expected_counts": expected,
        "actual_counts": after,
        "inventory_problem_count": len(inventory_problems),
        "inventory_problems_sample": inventory_problems[:50],
        "input_sha256": {path.relative_to(ROOT).as_posix(): sha256(path) for path in input_paths},
        "checks": {
            "csv_expected_counts_match_database": expected["nodes_by_type"] == actual["nodes_by_type"] and expected["edges_by_type"] == actual["edges_by_type"],
            "all_imported_relationships_have_source_release_assertion_and_evidence_id": after["source_assertion_missing_edge_count"] == 0,
            "all_build_relationships_use_versioned_identity": after["unversioned_build_member_count"] == 0,
            "isolated_nodes_are_zero": isolated_node_count == 0,
            "exact_node_and_relationship_inventory_matches": not inventory_problems,
            "candidate_target_s1_pathway_route_count_matches_csv_audit": after["candidate_target_pathway_routes"] == {"route_count": 1422, "candidate_count": 22, "drug_count": 22, "target_gene_count": 41},
        },
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "graph_build_id": graph_build_id, "import_runs": report["import_runs"], "node_count": unique_node_count, "edge_count": sum(expected_edge_counts.values()), "inventory_problems": len(inventory_problems), "report": REPORT.relative_to(ROOT).as_posix()}, ensure_ascii=False, indent=2))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="按 GraphBuild 范围验收 Neo4j 导入")
    parser.add_argument("--graph-build-id", default=None, help="Optional explicit ID; defaults to an input-hash-derived ID")
    parser.add_argument("--project-id", default="PHASE_B_VERIFICATION")
    args = parser.parse_args()
    main(args.graph_build_id, args.project_id)
