from kg.scripts.verify_neo4j_import import compare_graph_inventory
from kg.scripts.verify_neo4j_import import derive_graph_build_id


def _node(node_id="N1", **overrides):
    properties = {"id": node_id, "label": "Drug A", "type": "Drug"}
    properties.update(overrides)
    return {"label": "Drug", "properties": properties}


def _relationship(relationship_id="R1", **overrides):
    properties = {"id": relationship_id, "graph_build_ids": ["GRAPH_BUILD:test"], "build_key": "key", "input_sha256": "sha"}
    properties.update(overrides)
    return {"type": "TARGETS", "properties": properties}


def _actual_node(node_id="N1", labels=None, **overrides):
    properties = {"id": node_id, "label": "Drug A", "type": "Drug"}
    properties.update(overrides)
    return {"labels": labels or ["Drug"], "properties": properties}


def _actual_relationship(relationship_id="R1", **overrides):
    properties = {"id": relationship_id, "graph_build_ids": ["GRAPH_BUILD:test"], "build_key": "key", "input_sha256": "sha"}
    properties.update(overrides)
    return {"type": "TARGETS", "properties": properties}


def test_compare_inventory_accepts_exact_match_and_detects_drift():
    expected_nodes = {"N1": _node()}
    expected_relationships = {"R1": _relationship()}
    actual = {"nodes": {"N1": _actual_node()}, "relationships": {"R1": _actual_relationship()}}
    assert compare_graph_inventory(expected_nodes, expected_relationships, actual) == []

    assert compare_graph_inventory(expected_nodes, expected_relationships, {"nodes": {}, "relationships": {"R1": _actual_relationship()}})
    assert compare_graph_inventory(expected_nodes, expected_relationships, {"nodes": {"N1": _actual_node(label="Old")}, "relationships": {"R1": _actual_relationship()}})
    assert compare_graph_inventory(expected_nodes, expected_relationships, {"nodes": {"N1": _actual_node()}, "relationships": {}})
    assert compare_graph_inventory(expected_nodes, expected_relationships, {"nodes": {"N1": _actual_node()}, "relationships": {"R1": _actual_relationship(build_key="changed")}})
    assert compare_graph_inventory(expected_nodes, expected_relationships, {"nodes": {"N1": _actual_node()}, "relationships": {"R1": _actual_relationship(), "R2": _actual_relationship("R2")}})


def test_compare_inventory_detects_label_and_type_drift():
    expected_nodes = {"N1": _node()}
    expected_relationships = {"R1": _relationship()}
    actual = {"nodes": {"N1": _actual_node(labels=["Gene"])}, "relationships": {"R1": {"type": "REFERENCES_DRUG", "properties": _relationship()["properties"]}}}
    problems = compare_graph_inventory(expected_nodes, expected_relationships, actual)
    assert any("node label drift" in problem for problem in problems)
    assert any("relationship type drift" in problem for problem in problems)


def test_derived_build_id_changes_with_input_hashes():
    first = derive_graph_build_id({"subgraph_nodes.csv": "a" * 64})
    second = derive_graph_build_id({"subgraph_nodes.csv": "b" * 64})
    assert first != second
    assert first.startswith("GRAPH_BUILD:VERIFY_PHASE_B:")
