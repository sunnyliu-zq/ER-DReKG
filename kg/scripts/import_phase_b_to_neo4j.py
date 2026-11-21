from __future__ import annotations

import csv
import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

# Project S1 enrichment assertions can be much larger than the csv module's
# 128 KiB default field limit; keep complete, auditable source assertions.
csv.field_size_limit(min(sys.maxsize, 2_147_483_647))

ROOT = Path(__file__).resolve().parents[2]
SUBGRAPH = ROOT / "experiments" / "platform_phase_b"
PATHWAY_EXTENSION = ROOT / "experiments" / "platform_phase_b_pathways"
ALLOWED_LABELS = {"Drug", "Gene", "Pathway", "GeneSet", "Candidate", "DiseaseContext", "Disease", "S2Condition"}
RELATION_MAP = {
    "DRG-GEN": "TARGETS",
    "PWY-GEN": "CONTAINS_GENE",
    "PROJECT-HAS_CANDIDATE": "HAS_CANDIDATE",
    "PROJECT-REFERENCES_DRUG": "REFERENCES_DRUG",
    "PROJECT-HAS_DEG": "HAS_DEG",
    "S1-ENRICHED-PATHWAY": "HAS_ENRICHED_PATHWAY",
    "S1-ENRICHED-GENE": "HAS_ENRICHED_GENE",
    "PROJECT-ABOUT_DISEASE": "ABOUT_DISEASE",
    "PROJECT-HAS_S2_CONDITION": "HAS_S2_CONDITION",
}


def to_neo4j_properties(values: dict) -> dict:
    """Convert JSON node properties to Neo4j's scalar/list property model."""
    result = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, (str, bool, int, float)):
            result[key] = value
        elif isinstance(value, list):
            if not value:
                continue
            if all(isinstance(item, (str, bool, int, float)) and item is not None for item in value):
                types = {type(item) for item in value}
                numeric_types = {int, float}
                if len(types) == 1 or types.issubset(numeric_types):
                    result[key] = value
                else:
                    result[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
            else:
                result[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            result[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return result


class GraphImportPreflightError(RuntimeError):
    """Raised before any write when existing graph state would be modified."""


class GraphImportVerificationError(RuntimeError):
    """Raised after writes when the persisted build does not match the input."""


class GraphImportLockTimeout(RuntimeError):
    """Raised when another importer holds the project import lock."""


def canonical_properties(properties: dict) -> str:
    """Canonical representation used for exact node/relationship comparison."""
    return json.dumps(properties, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


GRAPH_INPUT_FILENAMES = {"subgraph_nodes.csv", "subgraph_edges.csv", "pathway_nodes.csv", "gene_nodes.csv", "pathway_edges.csv"}


def read_rows_with_hash(path: Path) -> tuple[list[dict[str, str]], str | None]:
    """Read a CSV once and hash the exact bytes that were parsed.

    Parsing from the same byte buffer closes the gap between hashing and row
    parsing inside the importer.
    """
    if not path.is_file():
        return [], None
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"Graph input is not valid UTF-8: {path}") from error
    return list(csv.DictReader(io.StringIO(text, newline=""))), hashlib.sha256(data).hexdigest()


def read_graph_inputs(subgraph_dir: Path, pathway_extension_dir: Path | None) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, str]]:
    """Read node/edge CSVs and return basename -> SHA-256 for every file read."""
    node_rows, node_hash = read_rows_with_hash(subgraph_dir / "subgraph_nodes.csv")
    edge_rows, edge_hash = read_rows_with_hash(subgraph_dir / "subgraph_edges.csv")
    input_hashes: dict[str, str] = {}
    if node_hash:
        input_hashes["subgraph_nodes.csv"] = node_hash
    if edge_hash:
        input_hashes["subgraph_edges.csv"] = edge_hash
    if pathway_extension_dir and pathway_extension_dir.exists():
        for filename in ["pathway_nodes.csv", "gene_nodes.csv"]:
            rows, digest = read_rows_with_hash(pathway_extension_dir / filename)
            if digest:
                input_hashes[filename] = digest
            node_rows.extend(rows)
        rows, digest = read_rows_with_hash(pathway_extension_dir / "pathway_edges.csv")
        if digest:
            input_hashes["pathway_edges.csv"] = digest
        edge_rows.extend(rows)
    return node_rows, edge_rows, input_hashes


def input_fingerprint(input_hashes: dict[str, str]) -> str:
    payload = json.dumps(sorted(input_hashes.items()), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def expected_hash_map(expected_hashes: dict[str, str]) -> dict[str, str]:
    """Map backend hash keys (possibly relative paths) to input basenames."""
    by_name: dict[str, str] = {}
    for key, value in expected_hashes.items():
        name = Path(str(key)).name
        prior = by_name.setdefault(name, value)
        if prior != value:
            raise GraphImportPreflightError(f"Conflicting expected hashes for input basename {name}")
    return by_name


def verify_input_hashes(actual_hashes: dict[str, str], expected_hashes_json: str | None) -> None:
    """Re-verify the parsed input bytes against the backend-registered hashes.

    This runs before any database connection or write and closes the TOCTOU window
    between backend bundle verification and importer file reads.
    """
    if expected_hashes_json is None:
        return
    try:
        expected_hashes = json.loads(expected_hashes_json)
    except json.JSONDecodeError as error:
        raise GraphImportPreflightError("--expected-input-hashes-json is not valid JSON") from error
    if not isinstance(expected_hashes, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in expected_hashes.items()):
        raise GraphImportPreflightError("--expected-input-hashes-json must be a string-to-string JSON object")
    expected_by_name = expected_hash_map(expected_hashes)
    problems: list[str] = []
    for name, digest in actual_hashes.items():
        if name not in expected_by_name:
            problems.append(f"{name}: read but not covered by expected hashes")
        elif expected_by_name[name] != digest:
            problems.append(f"{name}: SHA-256 mismatch")
    for name in GRAPH_INPUT_FILENAMES:
        if name in expected_by_name and name not in actual_hashes:
            problems.append(f"{name}: expected but missing")
    if problems:
        raise GraphImportPreflightError("Input hash verification failed: " + "; ".join(sorted(problems)))


def relationship_id_is_versioned(relationship_id: str | None, graph_build_id: str) -> bool:
    return str(relationship_id or "").endswith(f"|{graph_build_id}")


def expected_relationship_map(edges_by_relation: dict[tuple[str, str, str], list[dict]], graph_build_id: str) -> dict[str, dict]:
    """Return {relationship_id: {type, properties}} for the exact persisted form."""
    expected: dict[str, dict] = {}
    for (relation, _from_label, _to_label), rows in edges_by_relation.items():
        for row in rows:
            relationship_id = row["id"]
            if relationship_id in expected:
                raise GraphImportPreflightError(f"Duplicate relationship identity in import batch: {relationship_id}")
            properties = {key: value for key, value in row.items() if key != "graph_build_id"}
            properties["graph_build_ids"] = [graph_build_id]
            expected[relationship_id] = {"type": relation, "properties": properties}
    return expected


def validate_existing_relationships(expected_relationships: dict[str, dict], existing_rows: list[dict], *, allow_missing: bool) -> None:
    """Compare existing build relationships with the exact expected inventory.

    Existing relationships not in the input, or any property/type drift, always
    fail. Missing expected relationships are allowed during preflight (resume)
    and forbidden during post-write verification.
    """
    error_type = GraphImportPreflightError if allow_missing else GraphImportVerificationError
    existing: dict[str, dict] = {}
    duplicates: list[str] = []
    for row in existing_rows:
        relationship_id = row.get("id")
        if relationship_id in existing:
            duplicates.append(str(relationship_id))
            continue
        existing[relationship_id] = row
    if duplicates:
        preview = ", ".join(duplicates[:10])
        raise error_type(f"Duplicate existing relationship ID(s): {preview}")

    stale = sorted(str(relationship_id) for relationship_id in set(existing) - set(expected_relationships))
    if stale:
        preview = ", ".join(stale[:10])
        more = "" if len(stale) <= 10 else f", ... {len(stale) - 10} more"
        raise error_type(f"Existing relationships not present in current input: {preview}{more}")

    missing = sorted(set(expected_relationships) - set(existing))
    if missing and not allow_missing:
        preview = ", ".join(missing[:10])
        more = "" if len(missing) <= 10 else f", ... {len(missing) - 10} more"
        raise error_type(f"Expected relationships missing after write: {preview}{more}")

    problems: list[str] = []
    for relationship_id in set(expected_relationships) & set(existing):
        expected = expected_relationships[relationship_id]
        found = existing[relationship_id]
        if found.get("type") != expected["type"]:
            problems.append(f"{relationship_id}: relationship type drift")
            continue
        if canonical_properties(found.get("properties") or {}) != canonical_properties(expected["properties"]):
            problems.append(f"{relationship_id}: property drift")
    if problems:
        preview = "; ".join(problems[:20])
        more = "" if len(problems) <= 20 else f"; ... {len(problems) - 20} more"
        raise error_type(f"Relationship inventory mismatch: {preview}{more}")


def expected_node_map(nodes_by_label: dict[str, list[dict]]) -> dict[str, dict]:
    """Return {node_id: {label, properties}} for every node this build would write."""
    expected: dict[str, dict] = {}
    for label, rows in nodes_by_label.items():
        for row in rows:
            node_id = row["id"]
            if node_id in expected:
                raise GraphImportPreflightError(
                    f"Node ID {node_id} appears more than once in the same import batch"
                )
            expected[node_id] = {"label": label, "properties": dict(row)}
    return expected


def validate_existing_nodes(expected_nodes: dict[str, dict], existing_rows: list[dict]) -> None:
    """Fail closed when an existing node ID has drift in labels or properties.

    A shared node may only be reused when the database already contains exactly
    one node with the same ID, the same label set, and byte-for-byte equivalent
    canonical properties. Missing IDs are new nodes and are allowed.
    """
    existing: dict[str, list[dict]] = defaultdict(list)
    for row in existing_rows:
        existing[row["id"]].append(row)

    problems: list[str] = []
    for node_id, node in expected_nodes.items():
        matches = existing.get(node_id, [])
        if not matches:
            continue
        if len(matches) != 1:
            problems.append(f"{node_id}: expected one existing node, found {len(matches)}")
            continue
        found = matches[0]
        found_labels = set(found.get("labels") or [])
        if found_labels != {node["label"]}:
            problems.append(
                f"{node_id}: label drift {sorted(found_labels)} != [{node['label']}]"
            )
            continue
        if canonical_properties(found.get("properties") or {}) != canonical_properties(node["properties"]):
            problems.append(f"{node_id}: property drift")
    if problems:
        preview = "; ".join(problems[:20])
        more = "" if len(problems) <= 20 else f"; ... {len(problems) - 20} more"
        raise GraphImportPreflightError(
            "Existing shared node drift detected; refusing to modify the accepted graph: "
            f"{preview}{more}"
        )


def validate_build_membership(graph_build_id: str, relationship_ids: list[str]) -> None:
    """Reject re-entry when any relationship already belongs to this build in legacy format.

    Legacy relationships identify themselves without a build suffix. If one is
    found for the requested build ID, importing with the new relationship ID
    scheme would create a parallel edge and inflate the old build's counts.
    """
    legacy = [str(relationship_id or "") for relationship_id in relationship_ids if not relationship_id_is_versioned(relationship_id, graph_build_id)]
    if legacy:
        preview = ", ".join(legacy[:10])
        more = "" if len(legacy) <= 10 else f", ... {len(legacy) - 10} more"
        raise GraphImportPreflightError(
            f"GraphBuild {graph_build_id} already has {len(legacy)} unversioned relationship member(s) "
            f"({preview}{more}); refusing to create duplicate versioned edges"
        )


def preflight_existing_graph(session, graph_build_id: str, expected_nodes: dict[str, dict], expected_relationships: dict[str, dict]) -> None:
    """Read-only checks that must pass before constraints or data are written."""
    node_ids = sorted(expected_nodes)
    existing_rows = (
        session.run(
            "MATCH (n) WHERE n.id IN $ids "
            "RETURN n.id AS id, labels(n) AS labels, properties(n) AS properties",
            ids=node_ids,
        ).data()
        if node_ids
        else []
    )
    validate_existing_nodes(expected_nodes, existing_rows)
    membership_rows = session.run(
        "MATCH ()-[r]->() WHERE $graph_build_id IN coalesce(r.graph_build_ids, []) "
        "RETURN r.id AS id, type(r) AS type, properties(r) AS properties",
        graph_build_id=graph_build_id,
    ).data()
    validate_build_membership(graph_build_id, [row.get("id") for row in membership_rows])
    validate_existing_relationships(expected_relationships, membership_rows, allow_missing=True)


def verify_written_relationships(session, graph_build_id: str, expected_relationships: dict[str, dict]) -> None:
    """Read back the persisted build and require an exact relationship inventory."""
    written_rows = session.run(
        "MATCH ()-[r]->() WHERE $graph_build_id IN coalesce(r.graph_build_ids, []) "
        "RETURN r.id AS id, type(r) AS type, properties(r) AS properties",
        graph_build_id=graph_build_id,
    ).data()
    validate_build_membership(graph_build_id, [row.get("id") for row in written_rows])
    validate_existing_relationships(expected_relationships, written_rows, allow_missing=False)


def relationship_identity(source_id: str, relation: str, target_id: str, source_release: str, graph_build_id: str) -> str:
    """Relationship identity is scoped to one GraphBuild.

    Source-release identity is retained, and the build suffix guarantees that a
    new build never merges into or mutates an already accepted relationship.
    """
    return f"{source_id}|{relation}|{target_id}|{source_release}|{graph_build_id}"


def _lock_path(project_id: str | None, graph_build_id: str) -> Path:
    token = project_id or graph_build_id
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"diseasegraph-graph-import-{digest}.lock"


def _try_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def project_import_lock(project_id: str | None, graph_build_id: str, timeout: float = 120.0):
    """Serialize graph imports per project using a cross-process OS file lock."""
    handle = _lock_path(project_id, graph_build_id).open("a+b")
    locked = False
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                _try_lock(handle)
                locked = True
                break
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise GraphImportLockTimeout(
                        f"Timed out after {timeout:.0f}s waiting for project graph import lock"
                    ) from error
                time.sleep(0.2)
        yield
    finally:
        try:
            if locked:
                _unlock(handle)
        finally:
            handle.close()


def prepare_biocypher_import_rows(node_rows: list[dict], edge_rows: list[dict], graph_build_id: str, build_key: str, input_sha256: str):
    """Translate bundle rows with BioCypher and prepare versioned MERGE rows."""
    if not graph_build_id or not build_key or not input_sha256:
        raise ValueError("graph_build_id, build_key and input_sha256 are required for a versioned import")
    try:
        from .biocypher_adapter import translate_graph_rows
    except ImportError:  # direct CLI execution
        from biocypher_adapter import translate_graph_rows
    translated_nodes, translated_edges, biocypher_report = translate_graph_rows(node_rows, edge_rows)

    node_label_by_id: dict[str, str] = {}
    node_display_by_id = {row["id"]: row["label"] for row in node_rows}
    nodes_by_label = defaultdict(list)
    for node in translated_nodes:
        if node.node_label not in ALLOWED_LABELS:
            raise ValueError(f"Unapproved BioCypher node label: {node.node_label}")
        previous = node_label_by_id.setdefault(node.node_id, node.node_label)
        if previous != node.node_label:
            raise ValueError(f"Node ID {node.node_id} appears under both {previous} and {node.node_label}")
        props = to_neo4j_properties(node.properties)
        props.update({"id": node.node_id, "label": node_display_by_id.get(node.node_id) or node.node_id, "type": node.node_label})
        if node.node_label in {"Candidate", "DiseaseContext", "S2Condition"}:
            props.setdefault("project_id", "PROJECT:ERPOS_GSE42568")
        nodes_by_label[node.node_label].append(props)

    edges_by_relation = defaultdict(list)
    allowed_relations = set(RELATION_MAP.values())
    for source_row, edge in zip(edge_rows, translated_edges):
        relation = edge.relationship_label
        if relation not in allowed_relations:
            raise ValueError(f"Unapproved BioCypher relationship: {relation}")
        source_label = node_label_by_id.get(edge.source_id)
        target_label = node_label_by_id.get(edge.target_id)
        if not source_label or not target_label:
            raise ValueError(f"Relationship endpoint missing from BioCypher nodes: {edge.source_id} -> {edge.target_id}")
        source_assertion = json.loads(source_row["properties_json"] or "{}")
        input_relation = source_row["label"]
        source_release = source_row["source_release"]
        edge_key = (relation, source_label, target_label)
        evidence_id = hashlib.sha256(f"{source_row['from']}|{input_relation}|{source_row['to']}|{source_release}".encode()).hexdigest()
        edges_by_relation[edge_key].append({
            "from": edge.source_id,
            "to": edge.target_id,
            "id": relationship_identity(edge.source_id, relation, edge.target_id, source_release, graph_build_id),
            "evidence_ids": [evidence_id],
            "source_release": source_release,
            "source_assertion_json": json.dumps(source_assertion, ensure_ascii=False, sort_keys=True),
            "relation_label": input_relation,
            "undirected": source_row["undirected"].lower() == "true",
            "build_key": build_key,
            "input_sha256": input_sha256,
            "graph_build_id": graph_build_id,
        })
    return nodes_by_label, edges_by_relation, biocypher_report


def write_graph_rows(session, nodes_by_label: dict[str, list[dict]], edges_by_relation: dict[tuple[str, str, str], list[dict]]) -> int:
    """Write prepared rows. Callers must run preflight first."""
    for label in sorted(ALLOWED_LABELS):
        session.run(f"CREATE CONSTRAINT {label.lower()}_id_unique IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE").consume()
    for label, rows in nodes_by_label.items():
        # Existing shared nodes were validated in preflight and must never be rewritten.
        query = f"UNWIND $rows AS row MERGE (n:{label} {{id: row.id}}) ON CREATE SET n += row"
        session.run(query, rows=rows).consume()
    for (relation, from_label, to_label), rows in edges_by_relation.items():
        query = f"UNWIND $rows AS row MATCH (a:{from_label} {{id: row.from}}) MATCH (b:{to_label} {{id: row.to}}) MERGE (a)-[r:{relation} {{id: row.id}}]->(b) SET r += row SET r.graph_build_ids = reduce(acc = [], value IN coalesce(r.graph_build_ids, []) + [row.graph_build_id] | CASE WHEN value IN acc THEN acc ELSE acc + value END) REMOVE r.graph_build_id"
        session.run(query, rows=rows).consume()
    return session.run("MATCH (n) WHERE n.id STARTS WITH 'CANDIDATE:' RETURN count(n) AS candidates").single()["candidates"]


def import_prepared_rows(session, graph_build_id: str, nodes_by_label: dict[str, list[dict]], edges_by_relation: dict[tuple[str, str, str], list[dict]]) -> int:
    """Preflight, write, then verify the persisted relationship inventory."""
    expected_relationships = expected_relationship_map(edges_by_relation, graph_build_id)
    preflight_existing_graph(session, graph_build_id, expected_node_map(nodes_by_label), expected_relationships)
    result = write_graph_rows(session, nodes_by_label, edges_by_relation)
    verify_written_relationships(session, graph_build_id, expected_relationships)
    return result


def main(
    graph_build_id: str = "GRAPH_BUILD:LEGACY_PHASE_B",
    subgraph_dir: Path = SUBGRAPH,
    pathway_extension_dir: Path | None = PATHWAY_EXTENSION,
    project_id: str | None = None,
    lock_timeout: float = 120.0,
    build_key: str | None = None,
    expected_input_hashes_json: str | None = None,
) -> None:
    # BioCypher translates the actual entities consumed by the importer.
    # Persistence remains custom Cypher so relationship identity includes the
    # source release plus GraphBuild, keeping each build's edges independent.
    node_rows, edge_rows, input_hashes = read_graph_inputs(subgraph_dir, pathway_extension_dir)
    verify_input_hashes(input_hashes, expected_input_hashes_json)
    input_sha256 = input_fingerprint(input_hashes)
    effective_build_key = build_key or input_sha256
    nodes_by_label, edges_by_relation, biocypher_report = prepare_biocypher_import_rows(
        node_rows, edge_rows, graph_build_id, effective_build_key, input_sha256
    )

    # Connect only after the complete BioCypher translation and validation pass.
    from neo4j import GraphDatabase
    uri = os.environ["NEO4J_URI"]
    user = os.environ["NEO4J_USER"]
    password = os.environ["NEO4J_PASSWORD"]
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
        with project_import_lock(project_id, graph_build_id, lock_timeout):
            with driver.session(database=database) as session:
                check = import_prepared_rows(session, graph_build_id, nodes_by_label, edges_by_relation)
                edge_count_labels = {f"{relation} ({from_label}->{to_label})": len(rows) for (relation, from_label, to_label), rows in edges_by_relation.items()}
                print(json.dumps({"status": "PASS", "biocypher": biocypher_report, "node_counts_by_type": {k: len(v) for k, v in nodes_by_label.items()}, "edge_counts_by_type": edge_count_labels, "candidate_nodes_in_db": check}, ensure_ascii=False, indent=2))
    finally:
        driver.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="将审计后的 Phase B 子图按版本幂等导入 Neo4j")
    parser.add_argument("--graph-build-id", default="GRAPH_BUILD:LEGACY_PHASE_B")
    parser.add_argument("--subgraph-dir", type=Path, default=SUBGRAPH)
    parser.add_argument("--pathway-extension-dir", type=Path, default=PATHWAY_EXTENSION)
    parser.add_argument("--project-id", default=None, help="Project-level import lock key; defaults to the GraphBuild ID")
    parser.add_argument("--lock-timeout", type=float, default=120.0, help="Seconds to wait for the project import lock")
    parser.add_argument("--build-key", default=None, help="Backend build key stored on every imported relationship")
    parser.add_argument("--expected-input-hashes-json", default=None, help="JSON object of expected input filename/path hashes")
    args = parser.parse_args()
    main(args.graph_build_id, args.subgraph_dir, args.pathway_extension_dir, args.project_id, args.lock_timeout, args.build_key, args.expected_input_hashes_json)
