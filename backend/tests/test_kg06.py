import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import kg06

PHASE_F_PROJECT = SimpleNamespace(
    slug="erpos-gse42568",
    project_id="3f18f4c1-713d-58c5-8a53-38eca5a5aaf9",
)
PHASE_F_GRAPH_BUILD = "GRAPH_BUILD:3f18f4c1-713d-58c5-8a53-38eca5a5aaf9:a6aabb2147cb5f5e"


def make_evidence(evidence_id: str, predicate: str = "TARGETS"):
    return SimpleNamespace(
        evidence_id=evidence_id,
        source_release="OptimusKG test release",
        source_record_id=f"source:{evidence_id}",
        subject_id="CHEMBL1",
        predicate=predicate,
        object_id="ENSG000001",
        source_url=None,
        assertion_sha256="a" * 64,
    )


def test_edge_evidence_preserves_actions_without_inventing_direction():
    evidence = make_evidence("e-target")
    edge = kg06._edge_evidence(
        {
            "id": "CHEMBL1|TARGETS|ENSG000001|source",
            "from": "CHEMBL1",
            "to": "ENSG000001",
            "source_release": "OptimusKG 3.2",
            "evidence_ids": ["e-target"],
            "source_assertion_json": json.dumps({"relation_assertions": [{"relation": "TARGET", "source": "DB"}]}),
        },
        "TARGETS",
        {"e-target": evidence},
    )
    assert edge["reported_actions"] == ["TARGET"]
    assert "no direction inferred" in edge["action_interpretation"]
    assert edge["evidence_complete"] is True
    assert edge["sources"][0]["source_record_id"] == "source:e-target"


def test_path_id_is_stable_and_degrees_are_kept_as_analysis_evidence():
    evidence = {f"e{i}": make_evidence(f"e{i}") for i in range(5)}
    properties = [
        {"id": "ctx|HAS_CANDIDATE|candidate|run", "from": "ctx", "to": "candidate", "evidence_ids": ["e0"], "source_release": "S2", "source_assertion_json": json.dumps({"source_record_id": "run:candidate"})},
        {"id": "candidate|REFERENCES_DRUG|CHEMBL1|run", "from": "candidate", "to": "CHEMBL1", "evidence_ids": ["e1"], "source_release": "LINCS", "source_assertion_json": json.dumps({"source_record_id": "LINCS:pert"})},
        {"id": "CHEMBL1|TARGETS|ENSG000001|kg", "from": "CHEMBL1", "to": "ENSG000001", "evidence_ids": ["e2"], "source_release": "OptimusKG", "source_assertion_json": json.dumps({"relation_assertions": [{"relation": "INHIBITOR"}]})},
        {"id": "GO:1|HAS_ENRICHED_GENE|ENSG000001|s1", "from": "GO:1", "to": "ENSG000001", "evidence_ids": ["e3"], "source_release": "S1 enrichment", "source_assertion_json": json.dumps({"analysis_assertions": [{"adjusted_p": 0.01}]})},
        {"id": "ctx|HAS_ENRICHED_PATHWAY|GO:1|s1", "from": "ctx", "to": "GO:1", "evidence_ids": ["e4"], "source_release": "S1 enrichment", "source_assertion_json": json.dumps({"analysis_assertions": [{"adjusted_p": 0.02}]})},
    ]
    row = {
        "candidate_id": "candidate", "drug_id": "CHEMBL1", "drug_label": "Drug A",
        "gene_id": "ENSG000001", "gene_label": "GENE1", "pathway_id": "GO:1", "pathway_label": "Pathway",
        "edge_properties": properties,
        "relation_types": ["HAS_CANDIDATE", "REFERENCES_DRUG", "TARGETS", "HAS_ENRICHED_GENE", "HAS_ENRICHED_PATHWAY"],
    }
    path_a = kg06._make_path(row, "s1_enrichment_overlap", "GRAPH_BUILD:test", evidence)
    path_b = kg06._make_path(row, "s1_enrichment_overlap", "GRAPH_BUILD:test", evidence)
    expected = hashlib.sha256(
        ("GRAPH_BUILD:test|s1_enrichment_overlap|" + "|".join(item["id"] for item in properties)).encode()
    ).hexdigest()
    assert path_a["path_id"] == path_b["path_id"] == expected
    assert [node["id"] for node in path_a["nodes"]] == ["candidate", "CHEMBL1", "ENSG000001", "GO:1", "ctx"]
    assert path_a["pathway_adjusted_p_values"] == [0.01, 0.02]
    assert path_a["reported_actions"] == ["INHIBITOR"]
    assert path_a["evidence_complete"] is True


def test_kg06_cypher_is_fixed_and_graphbuild_scoped():
    for query in [kg06.TARGET_QUERY, kg06.PUBLIC_PATHWAY_QUERY, kg06.S1_ENRICHMENT_QUERY, kg06.S1_DEG_QUERY]:
        assert "$project_slug" in query
        assert "$pert_id" in query
        assert "$graph_build_id" in query
        assert "LIMIT " in query
    assert "ProteinTarget" not in kg06.TARGET_QUERY


def test_phase_f_graph_build_status_fails_closed_when_manifest_id_missing():
    assert kg06._phase_f_graph_build_status({}, "GRAPH_BUILD:expected") == (False, "missing_graph_build_id")
    assert kg06._phase_f_graph_build_status({"graph_build_id": "GRAPH_BUILD:other"}, "GRAPH_BUILD:expected") == (
        False,
        "graph_build_mismatch",
    )


def test_phase_f_matching_build_keeps_existing_phase_f_behavior():
    row, provenance = kg06._read_manifested_phase_f_row(
        "BRD-K44227013", PHASE_F_PROJECT, PHASE_F_GRAPH_BUILD
    )
    assert row is not None
    assert provenance["available"] is True
    assert provenance["kg_fields_available"] is True
    assert provenance["graph_build_matches"] is True
    assert row["kg_supported_route_count"] == "131"
    assert row["external_s1_median_rank"] == "3439.0"
    assert kg06._phase_f_joined_evidence(row, provenance)["kg_supported_route_count"] == "131"


def test_phase_f_other_build_isolates_kg_fields_but_keeps_independent_fields():
    row, provenance = kg06._read_manifested_phase_f_row(
        "BRD-K44227013",
        PHASE_F_PROJECT,
        "GRAPH_BUILD:3f18f4c1-713d-58c5-8a53-38eca5a5aaf9:a08001496a447f7f",
    )
    assert row is not None
    assert provenance["available"] is False
    assert provenance["kg_fields_available"] is False
    assert provenance["graph_build_matches"] is False
    assert provenance["reason"] == "graph_build_mismatch"
    assert provenance["independent_fields_retained"] is True
    assert provenance["kg_derived_fields_isolated"] is True
    assert all(not key.startswith("kg_") for key in row)
    assert row["external_s1_median_rank"] == "3439.0"
    assert "kg_supported_route_count" not in row
    assert kg06._phase_f_joined_evidence(row, provenance) == {
        "available": False,
        "reason": "graph_build_mismatch",
    }


def test_phase_f_missing_manifest_graph_build_id_fails_closed(monkeypatch):
    manifest = {
        "status": "PASS",
        "project_id": PHASE_F_PROJECT.project_id,
        "output_sha256": {
            "experiments/phase_f_integrated_evidence/candidate_evidence_with_kg.csv": "expected"
        },
    }
    monkeypatch.setattr(kg06, "json", SimpleNamespace(loads=lambda *_: manifest))
    monkeypatch.setattr(kg06, "sha256_file", lambda *_: "expected")
    row, provenance = kg06._read_manifested_phase_f_row(
        "BRD-K44227013", PHASE_F_PROJECT, "GRAPH_BUILD:expected"
    )
    assert row is not None
    assert provenance["available"] is False
    assert provenance["reason"] == "missing_graph_build_id"
    assert all(not key.startswith("kg_") for key in row)
    assert row["external_s1_median_rank"] == "3439.0"


def test_phase_f_sha_mismatch_still_fails_closed(monkeypatch):
    monkeypatch.setattr(kg06, "sha256_file", lambda *_: "bad-sha")
    row, provenance = kg06._read_manifested_phase_f_row(
        "BRD-K44227013", PHASE_F_PROJECT, PHASE_F_GRAPH_BUILD
    )
    assert row is None
    assert provenance["available"] is False
    assert provenance["reason"] == "hash_mismatch"


def test_build_evidence_package_rejects_expected_graph_build_mismatch_before_queries():
    project = SimpleNamespace(project_id="project-a")
    build = SimpleNamespace(project_id="project-a", status="materialized", graph_source="neo4j", graph_build_id="GRAPH_BUILD:B")
    with pytest.raises(ValueError, match="expected GraphBuild"):
        kg06.build_evidence_package(
            None,
            project,
            build,
            Path("."),
            "BRD-K44227013",
            expected_graph_build_id="GRAPH_BUILD:A",
        )


def test_build_evidence_package_rejects_non_neo4j_build_before_queries():
    project = SimpleNamespace(project_id="project-a")
    build = SimpleNamespace(project_id="project-a", status="materialized", graph_source="csv", graph_build_id="GRAPH_BUILD:csv")
    with pytest.raises(ValueError, match="not a Neo4j build"):
        kg06.build_evidence_package(
            None,
            project,
            build,
            Path("."),
            "BRD-K44227013",
        )
