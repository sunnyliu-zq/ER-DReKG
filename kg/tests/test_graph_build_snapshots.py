from kg.scripts.compare_graph_build_snapshots import _property_hash
from kg.scripts.compare_graph_build_snapshots import compare_graph_build_snapshots
from kg.scripts.compare_graph_build_snapshots import snapshot_graph_build


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def data(self):
        return self.rows


class _Session:
    def __init__(self, relationship_rows, node_rows):
        self.relationship_rows = relationship_rows
        self.node_rows = node_rows
        self.queries = []

    def run(self, query, **params):
        self.queries.append(query)
        if "RETURN r.id AS id, type(r) AS type, properties(r) AS properties" in query:
            return _Result(self.relationship_rows)
        return _Result(self.node_rows)


def _snapshot(node_properties=None, relationship_properties=None, graph_build_ids=None):
    node_properties = node_properties or {"id": "N1", "label": "Drug A", "type": "Drug"}
    relationship_properties = relationship_properties or {
        "id": "R1", "from": "N1", "to": "N2", "graph_build_ids": ["GRAPH_BUILD:test"],
    }
    return {
        "graph_build_id": "GRAPH_BUILD:test",
        "nodes": {
            "N1": {"labels": ["Drug"], "properties_sha256": _property_hash(node_properties)},
            "N2": {"labels": ["Gene"], "properties_sha256": "gene-hash"},
        },
        "relationships": {
            "R1": {
                "type": "TARGETS",
                "properties_sha256": _property_hash(relationship_properties),
                "graph_build_ids": graph_build_ids if graph_build_ids is not None else ["GRAPH_BUILD:test"],
            },
        },
    }


def test_snapshot_graph_build_captures_nodes_and_relationship_build_membership():
    session = _Session(
        relationship_rows=[{"id": "R1", "type": "TARGETS", "properties": {"id": "R1", "from": "N1", "to": "N2", "graph_build_ids": ["GRAPH_BUILD:test"]}}],
        node_rows=[
            {"id": "N1", "labels": ["Drug"], "properties": {"id": "N1", "label": "Drug A"}},
            {"id": "N2", "labels": ["Gene"], "properties": {"id": "N2", "label": "TP53"}},
        ],
    )
    snapshot = snapshot_graph_build(session, "GRAPH_BUILD:test")

    assert set(snapshot["nodes"]) == {"N1", "N2"}
    assert snapshot["relationships"]["R1"]["graph_build_ids"] == ["GRAPH_BUILD:test"]
    assert snapshot["relationships"]["R1"]["type"] == "TARGETS"


def test_compare_snapshots_detects_node_relationship_property_and_membership_changes():
    before = _snapshot()
    assert compare_graph_build_snapshots(before, before) == []

    node_changed = _snapshot(node_properties={"id": "N1", "label": "Old drug", "type": "Drug"})
    assert any("node properties changed: N1" in problem for problem in compare_graph_build_snapshots(before, node_changed))

    relationship_changed = _snapshot(relationship_properties={
        "id": "R1", "from": "N1", "to": "N2", "graph_build_ids": ["GRAPH_BUILD:test"], "build_key": "changed",
    })
    assert any("relationship properties changed: R1" in problem for problem in compare_graph_build_snapshots(before, relationship_changed))

    membership_changed = _snapshot(graph_build_ids=["GRAPH_BUILD:test", "GRAPH_BUILD:other"])
    assert any("graph_build_ids changed: R1" in problem for problem in compare_graph_build_snapshots(before, membership_changed))

    removed = _snapshot()
    removed["relationships"].clear()
    assert any("relationship removed: R1" in problem for problem in compare_graph_build_snapshots(before, removed))
