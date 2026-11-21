"""Read-only GraphBuild snapshots and before/after comparison.

This script never mutates Neo4j. It records the exact node/relationship property
hashes and relationship graph_build_ids for one build so an importer run can be
checked against an already accepted build.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

try:
    from .import_phase_b_to_neo4j import canonical_properties
except ImportError:  # direct CLI execution
    from import_phase_b_to_neo4j import canonical_properties

ROOT = Path(__file__).resolve().parents[2]


def _property_hash(properties: dict) -> str:
    return hashlib.sha256(canonical_properties(properties).encode("utf-8")).hexdigest()


def _relationship_key(row: dict) -> str:
    relationship_id = row.get("id")
    if relationship_id:
        return str(relationship_id)
    properties = row.get("properties") or {}
    return f"<null>|{row.get('type')}|{properties.get('from')}|{properties.get('to')}"


def snapshot_graph_build(session, graph_build_id: str, node_ids: list[str] | None = None) -> dict:
    """Read one build's relationships, nodes, and exact property hashes.

    By default nodes are the relationship endpoints. Pass node_ids to also cover
    nodes that have no relationship in this build.
    """
    relationship_rows = session.run(
        "MATCH ()-[r]->() WHERE $build_id IN coalesce(r.graph_build_ids, []) "
        "RETURN r.id AS id, type(r) AS type, properties(r) AS properties",
        build_id=graph_build_id,
    ).data()
    selected_node_ids = sorted(set(node_ids)) if node_ids is not None else sorted({
        node_id
        for row in relationship_rows
        for node_id in ((row.get("properties") or {}).get("from"), (row.get("properties") or {}).get("to"))
        if node_id
    })
    node_rows = session.run(
        "MATCH (n) WHERE n.id IN $ids "
        "RETURN n.id AS id, labels(n) AS labels, properties(n) AS properties",
        ids=selected_node_ids,
    ).data() if selected_node_ids else []
    return {
        "graph_build_id": graph_build_id,
        "nodes": {
            row["id"]: {
                "labels": sorted(row.get("labels") or []),
                "properties_sha256": _property_hash(row.get("properties") or {}),
            }
            for row in node_rows
        },
        "relationships": {
            _relationship_key(row): {
                "type": row.get("type"),
                "properties_sha256": _property_hash(row.get("properties") or {}),
                "graph_build_ids": sorted((row.get("properties") or {}).get("graph_build_ids") or []),
            }
            for row in relationship_rows
        },
    }


def compare_graph_build_snapshots(before: dict, after: dict) -> list[str]:
    """Return human-readable differences; empty means the snapshots are identical."""
    problems: list[str] = []
    if before.get("graph_build_id") != after.get("graph_build_id"):
        problems.append(f"graph_build_id changed: {before.get('graph_build_id')} -> {after.get('graph_build_id')}")

    before_nodes = before.get("nodes", {})
    after_nodes = after.get("nodes", {})
    for node_id in sorted(set(before_nodes) - set(after_nodes)):
        problems.append(f"node removed: {node_id}")
    for node_id in sorted(set(after_nodes) - set(before_nodes)):
        problems.append(f"node added: {node_id}")
    for node_id in sorted(set(before_nodes) & set(after_nodes)):
        if before_nodes[node_id].get("labels") != after_nodes[node_id].get("labels"):
            problems.append(f"node labels changed: {node_id}")
        if before_nodes[node_id].get("properties_sha256") != after_nodes[node_id].get("properties_sha256"):
            problems.append(f"node properties changed: {node_id}")

    before_relationships = before.get("relationships", {})
    after_relationships = after.get("relationships", {})
    for relationship_id in sorted(set(before_relationships) - set(after_relationships)):
        problems.append(f"relationship removed: {relationship_id}")
    for relationship_id in sorted(set(after_relationships) - set(before_relationships)):
        problems.append(f"relationship added: {relationship_id}")
    for relationship_id in sorted(set(before_relationships) & set(after_relationships)):
        before_relationship = before_relationships[relationship_id]
        after_relationship = after_relationships[relationship_id]
        if before_relationship.get("type") != after_relationship.get("type"):
            problems.append(f"relationship type changed: {relationship_id}")
        if before_relationship.get("properties_sha256") != after_relationship.get("properties_sha256"):
            problems.append(f"relationship properties changed: {relationship_id}")
        if before_relationship.get("graph_build_ids") != after_relationship.get("graph_build_ids"):
            problems.append(f"relationship graph_build_ids changed: {relationship_id}")
    return problems


def _load_env() -> dict[str, str]:
    values = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key and not key.startswith("#"):
                values[key] = value
    return values


def _read_node_ids(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return [line.strip() for line in text.splitlines() if line.strip()]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("node IDs file must be a JSON string array or one ID per line")
    return value


def _snapshot_command(graph_build_id: str, output: Path, node_ids_file: Path | None = None) -> None:
    from neo4j import GraphDatabase

    env = os.environ.copy()
    env.update(_load_env())
    node_ids = _read_node_ids(node_ids_file) if node_ids_file else None
    driver = GraphDatabase.driver(env["NEO4J_URI"], auth=(env["NEO4J_USER"], env["NEO4J_PASSWORD"]))
    try:
        with driver.session(database=env.get("NEO4J_DATABASE", "neo4j")) as session:
            snapshot = snapshot_graph_build(session, graph_build_id, node_ids)
    finally:
        driver.close()
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "PASS", "mode": "snapshot", "graph_build_id": graph_build_id, "output": str(output)}, ensure_ascii=False, indent=2))


def _compare_command(before_path: Path, after_path: Path) -> None:
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    problems = compare_graph_build_snapshots(before, after)
    print(json.dumps({"status": "PASS" if not problems else "FAIL", "mode": "compare", "differences": problems}, ensure_ascii=False, indent=2))
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="只读 GraphBuild 快照与前后比较")
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--graph-build-id", required=True)
    snapshot_parser.add_argument("--output", type=Path, required=True)
    snapshot_parser.add_argument("--node-ids-file", type=Path, default=None, help="Optional JSON array or newline list of node IDs, including isolated nodes")
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--before", type=Path, required=True)
    compare_parser.add_argument("--after", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "snapshot":
        _snapshot_command(args.graph_build_id, args.output, args.node_ids_file)
    else:
        _compare_command(args.before, args.after)
