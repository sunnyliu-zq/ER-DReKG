import csv
import json

import pytest

from kg.scripts.import_phase_b_to_neo4j import GraphImportPreflightError
from kg.scripts.import_phase_b_to_neo4j import GraphImportVerificationError
from kg.scripts.import_phase_b_to_neo4j import expected_node_map
from kg.scripts.import_phase_b_to_neo4j import expected_relationship_map
from kg.scripts.import_phase_b_to_neo4j import import_prepared_rows
from kg.scripts.import_phase_b_to_neo4j import input_fingerprint
from kg.scripts.import_phase_b_to_neo4j import prepare_biocypher_import_rows
from kg.scripts.import_phase_b_to_neo4j import read_graph_inputs
from kg.scripts.import_phase_b_to_neo4j import relationship_id_is_versioned
from kg.scripts.import_phase_b_to_neo4j import relationship_identity
from kg.scripts.import_phase_b_to_neo4j import to_neo4j_properties
from kg.scripts.import_phase_b_to_neo4j import validate_build_membership
from kg.scripts.import_phase_b_to_neo4j import validate_existing_nodes
from kg.scripts.import_phase_b_to_neo4j import validate_existing_relationships
from kg.scripts.import_phase_b_to_neo4j import verify_input_hashes


def test_nested_json_properties_are_encoded_for_neo4j():
    properties = to_neo4j_properties({
        "symbol": "TP53",
        "score_vector": [1, 2.5],
        "collections": ["GO_UP", "HALLMARK"],
        "analysis_assertions": [{"analysis": "GO_UP", "adjusted_p": 0.01}],
        "source_map": {"direct": ["PUBMED"]},
        "empty": None,
    })

    assert properties["symbol"] == "TP53"
    assert properties["score_vector"] == [1, 2.5]
    assert properties["collections"] == ["GO_UP", "HALLMARK"]
    assert properties["analysis_assertions"] == '[{"adjusted_p": 0.01, "analysis": "GO_UP"}]'
    assert properties["source_map"] == '{"direct": ["PUBMED"]}'
    assert "empty" not in properties


def test_importer_accepts_large_auditable_csv_assertions(tmp_path):
    value = "x" * (200 * 1024)
    path = tmp_path / "subgraph_edges.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["properties_json"])
        writer.writeheader(); writer.writerow({"properties_json": value})
    with path.open(encoding="utf-8", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert len(row["properties_json"]) == len(value)


def test_biocypher_normalized_import_preserves_source_release_identity_and_graph_build():
    nodes = [
        {"id": "DRUG:X", "type": "Drug", "label": "Drug X", "properties_json": "{}"},
        {"id": "ENSG000001", "type": "Gene", "label": "TP53", "properties_json": '{"symbol":"TP53"}'},
    ]
    edges = [
        {"from": "DRUG:X", "to": "ENSG000001", "label": "DRG-GEN", "relation": "TARGET", "undirected": "false", "source_release": "release-a", "properties_json": '{"source_assertions":[{"source":"db-a"}]}'},
        {"from": "DRUG:X", "to": "ENSG000001", "label": "DRG-GEN", "relation": "TARGET", "undirected": "false", "source_release": "release-b", "properties_json": '{"source_assertions":[{"source":"db-b"}]}'},
    ]

    nodes_by_type, edges_by_type, report = prepare_biocypher_import_rows(
        nodes, edges, "GRAPH_BUILD:test", "build-key-a", "input-sha-a"
    )

    assert report["status"] == "PASS"
    assert nodes_by_type["Gene"][0]["symbol"] == "TP53"
    rows = edges_by_type[("TARGETS", "Drug", "Gene")]
    assert {row["id"] for row in rows} == {
        "DRUG:X|TARGETS|ENSG000001|release-a|GRAPH_BUILD:test",
        "DRUG:X|TARGETS|ENSG000001|release-b|GRAPH_BUILD:test",
    }
    assert all(row["id"] == relationship_identity("DRUG:X", "TARGETS", "ENSG000001", row["source_release"], "GRAPH_BUILD:test") for row in rows)
    assert all(row["graph_build_id"] == "GRAPH_BUILD:test" for row in rows)
    assert all(row["build_key"] == "build-key-a" and row["input_sha256"] == "input-sha-a" for row in rows)
    assert {row["source_release"] for row in rows} == {"release-a", "release-b"}
    assert {json.loads(row["source_assertion_json"])["source_assertions"][0]["source"] for row in rows} == {"db-a", "db-b"}


def test_preflight_accepts_matching_existing_node():
    expected = expected_node_map({"Drug": [{"id": "CHEMBL1", "label": "Drug A", "type": "Drug", "name": "Drug A"}]})
    existing = [{"id": "CHEMBL1", "labels": ["Drug"], "properties": {"id": "CHEMBL1", "label": "Drug A", "type": "Drug", "name": "Drug A"}}]

    validate_existing_nodes(expected, existing)


def test_preflight_rejects_property_drift_before_any_write():
    nodes_by_label = {"Drug": [{"id": "CHEMBL1", "label": "Drug A", "type": "Drug", "name": "Drug A"}]}
    existing = [{"id": "CHEMBL1", "labels": ["Drug"], "properties": {"id": "CHEMBL1", "label": "Old drug", "type": "Drug", "name": "Old drug"}}]
    session = _PreflightSession(existing_nodes=existing)

    with pytest.raises(GraphImportPreflightError, match="property drift"):
        import_prepared_rows(session, "GRAPH_BUILD:new", nodes_by_label, {})

    assert session.queries
    assert all("CREATE CONSTRAINT" not in query and "MERGE" not in query for query in session.queries)


def test_preflight_rejects_label_and_duplicate_node_drift():
    expected = expected_node_map({"Drug": [{"id": "CHEMBL1", "label": "Drug A", "type": "Drug"}]})
    with pytest.raises(GraphImportPreflightError, match="label drift"):
        validate_existing_nodes(expected, [{"id": "CHEMBL1", "labels": ["Gene"], "properties": {"id": "CHEMBL1", "label": "Drug A", "type": "Drug"}}])
    with pytest.raises(GraphImportPreflightError, match="expected one existing node"):
        validate_existing_nodes(expected, [
            {"id": "CHEMBL1", "labels": ["Drug"], "properties": {"id": "CHEMBL1", "label": "Drug A", "type": "Drug"}},
            {"id": "CHEMBL1", "labels": ["Drug"], "properties": {"id": "CHEMBL1", "label": "Drug A", "type": "Drug"}},
        ])


def test_legacy_relationship_membership_is_rejected():
    validate_build_membership("GRAPH_BUILD:test", ["A|TARGETS|B|release|GRAPH_BUILD:test"])
    with pytest.raises(GraphImportPreflightError, match="unversioned relationship"):
        validate_build_membership("GRAPH_BUILD:test", ["A|TARGETS|B|release"])


def test_legacy_relationship_check_treats_null_id_as_legacy():
    assert relationship_id_is_versioned("A|TARGETS|B|release|GRAPH_BUILD:test", "GRAPH_BUILD:test")
    assert not relationship_id_is_versioned(None, "GRAPH_BUILD:test")
    with pytest.raises(GraphImportPreflightError, match="unversioned relationship"):
        validate_build_membership("GRAPH_BUILD:test", [None])


def test_legacy_relationship_preflight_makes_no_write_calls():
    session = _PreflightSession(existing_relationships=[{"id": "A|TARGETS|B|release", "type": "TARGETS", "properties": {}}])

    with pytest.raises(GraphImportPreflightError, match="unversioned relationship"):
        import_prepared_rows(session, "GRAPH_BUILD:new", {}, {})

    assert session.queries
    assert all("CREATE CONSTRAINT" not in query and "MERGE" not in query for query in session.queries)


def test_relationship_preflight_rejects_stale_or_changed_relationships():
    expected = expected_relationship_map({("TARGETS", "Drug", "Gene"): [_edge_row()]}, "GRAPH_BUILD:test")
    existing = _materialize(expected)
    validate_existing_relationships(expected, existing, allow_missing=True)

    stale = existing + [{"id": "STALE|TARGETS|X|release|GRAPH_BUILD:test", "type": "TARGETS", "properties": {}}]
    with pytest.raises(GraphImportPreflightError, match="not present in current input"):
        validate_existing_relationships(expected, stale, allow_missing=True)

    drift = _materialize(expected)
    drift[0]["properties"]["build_key"] = "different-build-key"
    with pytest.raises(GraphImportPreflightError, match="property drift"):
        validate_existing_relationships(expected, drift, allow_missing=True)

    type_drift = _materialize(expected)
    type_drift[0]["type"] = "REFERENCES_DRUG"
    with pytest.raises(GraphImportPreflightError, match="relationship type drift"):
        validate_existing_relationships(expected, type_drift, allow_missing=True)


def test_relationship_preflight_allows_missing_for_resume():
    expected = expected_relationship_map(
        {("TARGETS", "Drug", "Gene"): [_edge_row("REL-1"), _edge_row("REL-2")]},
        "GRAPH_BUILD:test",
    )
    existing = _materialize(expected)
    existing = [row for row in existing if row["id"] == _versioned("REL-1")]

    validate_existing_relationships(expected, existing, allow_missing=True)


def test_post_write_verification_rejects_missing_extra_or_changed_relationships():
    expected = expected_relationship_map({("TARGETS", "Drug", "Gene"): [_edge_row("REL-1")]}, "GRAPH_BUILD:test")
    session = _PreflightSession(written_relationships=_materialize(expected))
    assert import_prepared_rows(session, "GRAPH_BUILD:test", {}, {("TARGETS", "Drug", "Gene"): [_edge_row("REL-1")]}) == 1

    missing = _PreflightSession(written_relationships=[])
    with pytest.raises(GraphImportVerificationError, match="missing after write"):
        import_prepared_rows(missing, "GRAPH_BUILD:test", {}, {("TARGETS", "Drug", "Gene"): [_edge_row("REL-1")]})

    extra = _PreflightSession(written_relationships=_materialize(expected) + [{"id": _versioned("EXTRA"), "type": "TARGETS", "properties": {}}])
    with pytest.raises(GraphImportVerificationError, match="not present in current input"):
        import_prepared_rows(extra, "GRAPH_BUILD:test", {}, {("TARGETS", "Drug", "Gene"): [_edge_row("REL-1")]})

    changed = _materialize(expected)
    changed[0]["properties"]["input_sha256"] = "different-input"
    changed_session = _PreflightSession(written_relationships=changed)
    with pytest.raises(GraphImportVerificationError, match="property drift"):
        import_prepared_rows(changed_session, "GRAPH_BUILD:test", {}, {("TARGETS", "Drug", "Gene"): [_edge_row("REL-1")]})


def test_input_hash_verification_rejects_mismatch_and_missing_files():
    actual = {"subgraph_nodes.csv": "a" * 64, "subgraph_edges.csv": "b" * 64}
    verify_input_hashes(actual, json.dumps({"subgraph_nodes.csv": "a" * 64, "subgraph_edges.csv": "b" * 64}))
    with pytest.raises(GraphImportPreflightError, match="SHA-256 mismatch"):
        verify_input_hashes(actual, json.dumps({"subgraph_nodes.csv": "c" * 64, "subgraph_edges.csv": "b" * 64}))
    with pytest.raises(GraphImportPreflightError, match="expected but missing"):
        verify_input_hashes(actual, json.dumps({"subgraph_nodes.csv": "a" * 64, "subgraph_edges.csv": "b" * 64, "pathway_nodes.csv": "d" * 64}))
    with pytest.raises(GraphImportPreflightError, match="read but not covered"):
        verify_input_hashes(actual, json.dumps({"subgraph_nodes.csv": "a" * 64}))


def test_node_write_uses_on_create_set():
    nodes_by_label = {"Drug": [{"id": "CHEMBL1", "label": "Drug A", "type": "Drug", "name": "Drug A"}]}
    session = _PreflightSession()

    assert import_prepared_rows(session, "GRAPH_BUILD:new", nodes_by_label, {}) == 1
    node_writes = [query for query in session.queries if "MERGE (n:Drug" in query]
    assert len(node_writes) == 1
    assert "ON CREATE SET n += row" in node_writes[0]


def test_read_graph_inputs_hashes_the_bytes_used_for_parsing(tmp_path):
    (tmp_path / "subgraph_nodes.csv").write_text("id,type,label,properties_json\nN1,Drug,Drug A,{}\n", encoding="utf-8")
    (tmp_path / "subgraph_edges.csv").write_text("from,label,to,relation,source_release,undirected,properties_json\nN1,DRG-GEN,N2,TARGET,release,false,{}\n", encoding="utf-8")
    node_rows, edge_rows, input_hashes = read_graph_inputs(tmp_path, None)
    assert node_rows[0]["id"] == "N1" and edge_rows[0]["to"] == "N2"
    assert set(input_hashes) == {"subgraph_nodes.csv", "subgraph_edges.csv"}
    assert input_fingerprint(input_hashes) != input_fingerprint({**input_hashes, "subgraph_nodes.csv": "0" * 64})
    (tmp_path / "subgraph_nodes.csv").write_text("id,type,label,properties_json\nN1,Drug,Changed,{}\n", encoding="utf-8")
    _, _, changed_hashes = read_graph_inputs(tmp_path, None)
    assert changed_hashes["subgraph_nodes.csv"] != input_hashes["subgraph_nodes.csv"]


def _versioned(label):
    return f"{label}|TARGETS|B|release|GRAPH_BUILD:test"


def _edge_row(relationship_id="REL-1", **overrides):
    row = {
        "id": _versioned(relationship_id),
        "from": "DRUG:X",
        "to": "ENSG000001",
        "evidence_ids": ["evidence-1"],
        "source_release": "release-a",
        "source_assertion_json": '{"source":"test"}',
        "relation_label": "DRG-GEN",
        "undirected": False,
        "build_key": "build-key-a",
        "input_sha256": "input-sha-a",
        "graph_build_id": "GRAPH_BUILD:test",
    }
    row.update(overrides)
    return row


def _materialize(expected_relationships):
    return [
        {"id": relationship_id, "type": entry["type"], "properties": dict(entry["properties"])}
        for relationship_id, entry in expected_relationships.items()
    ]


class _PreflightResult:
    def __init__(self, rows):
        self.rows = rows

    def data(self):
        return self.rows

    def single(self):
        return self.rows[0] if self.rows else None

    def consume(self):
        return None


class _PreflightSession:
    def __init__(self, existing_nodes=None, existing_relationships=None, written_relationships=None):
        self.existing_nodes = existing_nodes or []
        self.existing_relationships = existing_relationships or []
        self.written_relationships = written_relationships if written_relationships is not None else []
        self.relationship_queries = 0
        self.queries = []

    def run(self, query, **params):
        self.queries.append(query)
        if "RETURN n.id AS id, labels(n) AS labels, properties(n) AS properties" in query:
            return _PreflightResult(self.existing_nodes)
        if "RETURN r.id AS id, type(r) AS type, properties(r) AS properties" in query:
            self.relationship_queries += 1
            rows = self.existing_relationships if self.relationship_queries == 1 else self.written_relationships
            return _PreflightResult(rows)
        if "STARTS WITH 'CANDIDATE:'" in query:
            return _PreflightResult([{"candidates": 1}])
        return _PreflightResult([])
