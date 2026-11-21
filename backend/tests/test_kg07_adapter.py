"""Stage 1 tests for the deterministic KG07 evidence adapter.

The fixture is assembled from the project's real archived KG06 paths, real
S2 condition rows, real Top10/Exp006/Phase F rows, and the real graph bundle's
Gene symbol mapping.  Expected Ponatinib counts are asserted, but none of those
counts are hardcoded in the adapter.
"""

from __future__ import annotations

import copy
import csv
import json
from pathlib import Path

import pytest

from app.kg07_adapter import (
    ACTION_CLASS_ORDER,
    DIMENSION_ORDER,
    LEGACY_SECTION_KEYS,
    build_kg07_evidence,
)
from app.interpretation.validator import backend_coverage, validate_explanation

ROOT = Path(__file__).resolve().parents[2]
PERT_ID = "BRD-K44227013"
ARCHIVE = ROOT / "experiments" / "kg06_mechanism_evidence" / "results" / "kg06_mechanism_paths.jsonl"
AUDIT_MANIFEST = ROOT / "experiments" / "kg06_mechanism_evidence" / "results" / "kg06_audit_manifest.json"
TOP10 = ROOT / "experiments" / "final_results" / "results" / "final_top10.csv"
EXP006 = ROOT / "experiments" / "exp006_ranking_reliability" / "results" / "candidate_drugs_top50.csv"
PHASE_F = ROOT / "experiments" / "phase_f_integrated_evidence" / "candidate_evidence_with_kg.csv"
REAL_PACKAGE = (
    ROOT
    / "experiments"
    / "kg06_mechanism_evidence"
    / "results"
    / "evidence_packages"
    / "BRD-K44227013.json"
)
REAL_V2_PACKAGE = (
    ROOT
    / "experiments"
    / "kg06_mechanism_evidence"
    / "results"
    / "evidence_packages_v2"
    / "BRD-K44227013.json"
)
S2 = (
    ROOT
    / "data"
    / "processed"
    / "projects"
    / "3f18f4c1-713d-58c5-8a53-38eca5a5aaf9"
    / "s2"
    / "c922f2b0-242e-5991-9f76-dcbc8f66ce57"
    / "drug_condition_summary.csv"
)
NODES = (
    ROOT
    / "data"
    / "processed"
    / "projects"
    / "3f18f4c1-713d-58c5-8a53-38eca5a5aaf9"
    / "graph_bundle"
    / "runs"
    / "39f13a70-03b7-4b05-bfb9-294898974973"
    / "subgraph_nodes.csv"
)


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _one_row(path: Path, key: str, value: str) -> dict[str, str]:
    rows = [row for row in _csv_rows(path) if row.get(key) == value]
    assert len(rows) == 1, f"expected exactly one {key}={value} in {path}, got {len(rows)}"
    return rows[0]


def _real_package() -> dict:
    if not ARCHIVE.exists():
        pytest.skip(f"real KG06 archive not available: {ARCHIVE}")

    with ARCHIVE.open("r", encoding="utf-8") as handle:
        archive_rows = [json.loads(line) for line in handle if line.strip()]
    paths = [row for row in archive_rows if row.get("pert_id") == PERT_ID]
    assert len(paths) == 157, "real archived Ponatinib path count changed"

    target_gene_ids = {
        str(row.get("target_gene_id"))
        for row in paths
        if row.get("target_gene_id")
    }
    symbol_map: dict[str, str] = {}
    for node in _csv_rows(NODES):
        node_id = node.get("id", "")
        if node.get("type") == "Gene" and node_id in target_gene_ids:
            symbol_map[node_id] = node.get("label", "")
    for path in paths:
        gene_id = str(path.get("target_gene_id") or "")
        if gene_id in symbol_map:
            # The archive strips path.nodes; add the symbol copied verbatim
            # from the real graph bundle, not an invented label.
            path["target_symbol"] = symbol_map[gene_id]

    manifest = json.loads(AUDIT_MANIFEST.read_text(encoding="utf-8"))
    graph_build_id = (
        manifest.get("schema", {}).get("graph_build_id")
        or manifest.get("graph_build_id")
    )
    assert graph_build_id

    top10 = _one_row(TOP10, "pert_id", PERT_ID)
    exp006 = _one_row(EXP006, "pert_id", PERT_ID)
    phase_f = _one_row(PHASE_F, "pert_id", PERT_ID)

    s2_conditions = []
    for row in _csv_rows(S2):
        if row.get("pert_id") != PERT_ID:
            continue
        condition = {
            "cell_id": row["cell_id"],
            "pert_type": row["pert_type"],
            "dose": row["dose"],
            "time": row["time"],
            "n_signatures": row["n_signatures"],
            "WTCS_median": row["WTCS_median"],
            "WTCS_mean": row["WTCS_mean"],
            "WTCS_sd": row["WTCS_sd"],
            "WTCS_mad": row["WTCS_mad"],
            "quality_cc_median": row["quality_cc_median"],
            "TAS_median": row["TAS_median"],
            "replicates_median": row["replicates_median"],
            "rank_within_condition": row["rank_within_condition"],
        }
        s2_conditions.append(
            {
                "condition_key": f"{row['cell_id']}|{row['pert_type']}|{row['dose']}|{row['time']}",
                "condition": condition,
                # The archived summary CSV has no EvidenceRecord IDs.  Leave
                # them absent; the adapter must report the gap, not invent IDs.
                "evidence_ids": [],
                "sources": [],
                "evidence_complete": False,
            }
        )
    assert len(s2_conditions) == 5, "real Ponatinib S2 condition count changed"

    candidate = {
        "candidate_id": top10.get("candidate_id", ""),
        "drug_name": top10.get("drug_name", "ponatinib"),
        "mapping_status": "mapped",
        "compound_id": top10.get("drug_id") or "CHEMBL1171837",
        "rank": top10.get("rank"),
        "n_signatures": top10.get("n_signatures"),
        "n_24h_dose_conditions": top10.get("n_24h_dose_conditions"),
        "negative_dose_fraction": top10.get("negative_dose_fraction"),
        "primary_dose": top10.get("primary_dose"),
        "source_dataset": top10.get("source_dataset") or exp006.get("source_dataset"),
    }
    screening = {
        "s2_analysis_run_id": "ANALYSIS_RUN:S2_LINCS:c922f2b0-242e-5991-9f76-dcbc8f66ce57",
        "s2_candidate": {
            "candidate_id": candidate["candidate_id"],
            "rank": top10.get("rank"),
            "score_name": "WTCS",
            "score_value": top10.get("WTCS_median"),
            "condition_count": len(s2_conditions),
            "conditions_source": "S2ConditionResult for the GraphBundle's exact S2 AnalysisRun",
        },
        "s2_conditions": s2_conditions,
        "exp006": exp006,
        "independent_validation_and_prism": {
            key: phase_f.get(key)
            for key in (
                "external_s1_median_rank",
                "external_s1_B200_median_rank",
                "external_s1_conditions",
                "Q2_condition_median_rank",
                "Q2_conditions",
                "erpos_mean_sensitivity",
                "erpos_median_sensitivity",
                "n_erpos_lines",
                "erneg_mean_sensitivity",
                "n_erneg_lines",
                "mcf7_sensitivity",
                "n_mcf7_conditions",
                "sensitivity_rank_erpos",
                "sensitivity_rank_mcf7",
                "PRISM_endpoint",
                "WTCS_PRISM_agreement_interpretation",
            )
        },
        "phase_f_joined_evidence": {
            key: phase_f.get(key)
            for key in (
                "mapping_method",
                "kg_mapping_status",
                "kg_supported_route_count",
                "kg_unique_target_gene_count",
                "kg_unique_pathway_count",
                "kg_interpretation",
            )
        },
    }

    all_evidence = sorted(
        {
            evidence_id
            for path in paths
            for evidence_id in (path.get("evidence_ids") or [])
        }
    )
    return {
        "package_version": "kg06-evidence-package-fixture-from-real-archive",
        "project_id": "3f18f4c1-713d-58c5-8a53-38eca5a5aaf9",
        "graph_build_id": graph_build_id,
        "pert_id": PERT_ID,
        "candidate": candidate,
        "screening_evidence": screening,
        "paths": paths,
        "evidence_gaps": [],
        "coverage": {
            "candidate_present": True,
            "compound_mapped": True,
            "direct_target_path_count": sum(
                path.get("route_type") == "direct_target" for path in paths
            ),
            "public_pathway_path_count": sum(
                path.get("route_type") == "public_pathway" for path in paths
            ),
            "s1_enrichment_overlap_path_count": sum(
                path.get("route_type") == "s1_enrichment_overlap" for path in paths
            ),
            "s1_deg_overlap_path_count": sum(
                path.get("route_type") == "s1_deg_overlap" for path in paths
            ),
            "unique_evidence_count": len(all_evidence),
            "s2_condition_evidence_count": 0,
            "s2_condition_count": len(s2_conditions),
            "evidence_integrity_pass": True,
        },
    }


@pytest.fixture(scope="module")
def package() -> dict:
    return _real_package()


@pytest.fixture(scope="module")
def summary(package: dict) -> dict:
    return build_kg07_evidence(package)


@pytest.fixture(scope="module")
def v2_package() -> dict:
    assert REAL_V2_PACKAGE.is_file(), f"missing real v2 package: {REAL_V2_PACKAGE}"
    return json.loads(REAL_V2_PACKAGE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def v2_summary(v2_package: dict) -> dict:
    return build_kg07_evidence(v2_package)


def test_real_fixture_has_expected_shape(package: dict) -> None:
    assert package["graph_build_id"].startswith("GRAPH_BUILD:")
    assert len(package["paths"]) == 157
    assert package["candidate"]["n_signatures"] == "1"
    assert len(package["screening_evidence"]["s2_conditions"]) == 5


def test_dimensions_and_graph_build_propagation(summary: dict) -> None:
    assert summary["dimension_order"] == list(DIMENSION_ORDER)
    assert summary["graph_build_id"].startswith("GRAPH_BUILD:")
    for key in DIMENSION_ORDER:
        assert summary["dimensions"][key]["graph_build_id"] == summary["graph_build_id"]
    assert summary["validator_bundle"]["graph_build_id"] == summary["graph_build_id"]
    assert summary["llm_input"]["graph_build_id"] == summary["graph_build_id"]
    assert summary["llm_validator_bundle"]["graph_build_id"] == summary["graph_build_id"]


def test_target_annotations_actions_and_symbols(summary: dict) -> None:
    target = summary["dimensions"]["target_coverage"]
    assert target["metrics"]["annotation_count"] == 21
    assert target["metrics"]["unique_target_gene_count"] == 21
    assert target["metrics"]["annotation_count_equals_unique_gene_count"] is True
    assert target["metrics"]["primary_action_counts"] == {
        "TARGET": 15,
        "ENZYME": 4,
        "TRANSPORTER": 2,
        "UNKNOWN": 0,
    }
    assert target["metrics"]["any_action_counts"] == {
        "TARGET": 15,
        "ENZYME": 4,
        "TRANSPORTER": 2,
        "UNKNOWN": 0,
    }
    assert target["metrics"]["action_class_order"] == list(ACTION_CLASS_ORDER)
    symbols = {row["symbol"] for row in target["annotations"]}
    assert {"ABL1", "BCR"}.issubset(symbols)
    assert len(target["targets"]) == 21
    assert all(row["path_ids"] for row in target["targets"])


def test_signature_and_dose_consistency(summary: dict) -> None:
    signature = summary["dimensions"]["signature_coverage"]["metrics"]
    assert signature["n_signatures"] == 1
    assert signature["n_dose_conditions"] == 5
    assert signature["replicates_median"] == 3.0
    assert signature["negative_dose_fraction"] == 0.2
    assert signature["single_consensus_signature"] is True
    assert signature["primary_dose"] == "10.0 um"

    dose = summary["dimensions"]["dose_consistency"]
    assert dose["metrics"]["condition_count"] == 5
    assert dose["metrics"]["negative_condition_count"] == 1
    assert dose["metrics"]["negative_doses"] == ["10.0 um"]
    assert dose["metrics"]["direction_flip"] is True
    assert dose["metrics"]["primary_dose_wtcs"] == pytest.approx(-0.516654198727056)
    assert [row["dose"] for row in dose["conditions"]] == [
        "0.12 um",
        "0.37 um",
        "1.11 um",
        "3.33 um",
        "10.0 um",
    ]


def test_s1_pathway_counts_and_representative_bound(summary: dict) -> None:
    pathway = summary["dimensions"]["s1_pathway_association"]
    assert pathway["metrics"]["record_count"] == 131
    assert pathway["metrics"]["unique_pathway_count"] == 99
    assert pathway["metrics"]["representative_path_count"] == 12
    assert pathway["metrics"]["representative_limit"] == 12
    assert pathway["metrics"]["representative_truncated"] is True
    assert pathway["metrics"]["s1_deg_overlap_record_count"] == 5
    assert pathway["pathways_total"] == 99
    assert len({row["pathway_id"] for row in pathway["representatives"]}) == 12
    assert len(summary["llm_input"]["pathways"]) == 12
    assert summary["llm_input"]["pathway_counts"]["unique_pathway_count"] == 99
    assert summary["llm_input"]["pathway_counts"]["record_count"] == 131


def test_independent_validation_is_deterministic_but_not_citable(summary: dict) -> None:
    independent = summary["dimensions"]["independent_validation"]
    assert independent["claims_allowed"] is False
    assert independent["status"] == "complete"
    assert independent["metrics"]["exp010"]
    assert independent["metrics"]["compz"]
    assert independent["metrics"]["prism"]
    assert independent["metrics"]["prism"]["PRISM_endpoint"]
    assert "不能称为独立药物实验" in " ".join(independent["caveats"])


def test_full_validator_bundle_keeps_complete_counts(summary: dict) -> None:
    bundle = summary["validator_bundle"]
    assert bundle["counts"]["direct_target_paths"] == 21
    assert bundle["counts"]["s1_enrichment_paths"] == 131
    assert bundle["counts"]["s1_deg_paths"] == 5
    assert bundle["counts"]["unique_evidence"] == 258
    assert bundle["counts"]["s2_conditions"] == 5
    assert bundle["counts"]["unique_target_genes"] == 21
    assert bundle["counts"]["unique_pathways"] == 99
    assert len(bundle["path_meta"]) == 157
    assert len(bundle["evidence_meta"]) == 258
    assert len(bundle["targets"]) == 21
    assert len(bundle["pathways"]) == 99
    assert len(bundle["s2_conditions"]["conditions"]) == 5
    assert len(bundle["allowed_path_ids"]) == 157
    assert len(bundle["allowed_evidence_ids"]) == 258
    assert bundle["path_route_types"]
    assert all(meta["path_ids"] for meta in bundle["evidence_meta"].values())


def test_real_v2_s2_evidence_is_certified_from_condition_sources(v2_summary: dict) -> None:
    full = v2_summary["validator_bundle"]
    scoped = v2_summary["llm_validator_bundle"]
    conditions = full["s2_conditions"]["conditions"]
    assert len(conditions) == 5
    assert full["s2_conditions"]["validation"] == {
        "valid": True,
        "certified_condition_count": 5,
        "invalid_evidence_ids": [],
    }
    assert scoped["s2_conditions"]["available"] is True
    assert len(scoped["s2_conditions"]["conditions"]) == 5
    assert len([meta for meta in scoped["evidence_meta"].values() if meta["kind"] == "s2"]) == 5

    for condition in conditions:
        condition_id = condition["condition_id"]
        assert condition_id.startswith("S2_CONDITION:")
        for evidence_id in condition["evidence_ids"]:
            assert evidence_id not in {
                value
                for path in v2_summary["validator_bundle"]["path_meta"].values()
                for value in path["evidence_ids"]
            }
            meta = full["evidence_meta"][evidence_id]
            assert meta["kind"] == "s2"
            assert meta["path_ids"] == []
            assert meta["route_types"] == []
            assert meta["target_genes"] == []
            assert meta["source_record_id"]
            assert meta["source_predicate"] == "HAS_S2_CONDITION"
            assert meta["condition_id"] == condition_id
            assert meta["verified_in_postgres"] is True
            assert evidence_id in full["allowed_evidence_ids"]
            assert evidence_id in scoped["allowed_evidence_ids"]

            source = next(
                row
                for row in condition["sources"]
                if row["evidence_id"] == evidence_id
            )
            assert source["predicate"] == "HAS_S2_CONDITION"
            assert source["object_id"] == condition_id
            assert source["verified_in_postgres"] is True


@pytest.mark.parametrize(
    "bad_id",
    [
        "fake-not-in-real-package",
        "ae2cddaa0d404a7ed81af5e26f68425e659022a86d807e344b311e288249361e",
        "0c41c7c24591437d8ca2b5453a9be7827c2ba02f580675faa9b9ee242382018e",
    ],
)
def test_unauthenticated_s2_ids_are_not_added_to_any_bundle(
    v2_package: dict, bad_id: str
) -> None:
    package = copy.deepcopy(v2_package)
    package["screening_evidence"]["s2_conditions"][0]["evidence_ids"].append(bad_id)
    summary = build_kg07_evidence(package)

    full = summary["validator_bundle"]
    scoped = summary["llm_validator_bundle"]
    assert bad_id not in {
        evidence_id
        for evidence_id, meta in full["evidence_meta"].items()
        if meta["kind"] == "s2"
    }
    assert bad_id not in {
        evidence_id
        for evidence_id, meta in scoped["evidence_meta"].items()
        if meta["kind"] == "s2"
    }
    assert bad_id not in {
        evidence_id
        for condition in full["s2_conditions"]["certified_conditions"]
        for evidence_id in condition["evidence_ids"]
    }
    assert full["s2_conditions"]["validation"]["valid"] is False
    assert bad_id in full["s2_conditions"]["validation"]["invalid_evidence_ids"]
    assert scoped["s2_conditions"]["conditions"] == []
    assert scoped["s2_conditions"]["available"] is False
    assert "s2_condition_evidence_unverified" in summary["metadata"]["missing_fields"]
    llm = summary["llm_input"]
    assert len(llm["pathways"]) == 12
    assert len(llm["targets"]) == 21
    assert len(llm["dose_conditions"]) == 5
    assert llm["target_annotation_count"] == 21
    assert llm["target_unique_gene_count"] == 21
    assert llm["pathway_counts"]["unique_pathway_count"] == 99
    assert llm["pathway_counts"]["record_count"] == 131
    assert "not_citable_in_stage_1" == llm["independent_validation_status"]
    assert "erpos_mean_sensitivity" not in json.dumps(llm, ensure_ascii=False)
    assert len(summary["llm_validator_bundle"]["pathways"]) == 12
    assert len(summary["llm_validator_bundle"]["targets"]) == 21
    assert len(summary["llm_validator_bundle"]["path_meta"]) < len(
        summary["validator_bundle"]["path_meta"]
    )


def _assert_s2_fail_closed(summary: dict, evidence_id: str) -> None:
    full = summary["validator_bundle"]
    scoped = summary["llm_validator_bundle"]
    assert full["s2_conditions"]["validation"]["valid"] is False
    assert full["s2_conditions"]["validation"]["certified_condition_count"] == 0
    assert evidence_id in full["s2_conditions"]["validation"]["invalid_evidence_ids"]
    assert full["s2_conditions"]["certified_conditions"] == []
    assert not any(
        meta["kind"] == "s2" for meta in full["evidence_meta"].values()
    )
    assert not any(
        meta["kind"] == "s2" for meta in scoped["evidence_meta"].values()
    )
    assert scoped["s2_conditions"]["available"] is False
    assert scoped["s2_conditions"]["conditions"] == []
    assert scoped["s2_conditions"]["certified_conditions"] == []
    assert evidence_id not in full["allowed_evidence_ids"]
    assert evidence_id not in scoped["allowed_evidence_ids"]


def test_duplicate_evidence_id_across_conditions_fails_closed(
    v2_package: dict,
) -> None:
    package = copy.deepcopy(v2_package)
    conditions = package["screening_evidence"]["s2_conditions"]
    shared_id = conditions[0]["evidence_ids"][0]
    source = copy.deepcopy(conditions[1]["sources"][0])
    source["evidence_id"] = shared_id
    source["object_id"] = conditions[1]["condition"]["id"]
    conditions[1]["evidence_ids"] = [shared_id]
    conditions[1]["sources"] = [source]

    summary = build_kg07_evidence(package)
    _assert_s2_fail_closed(summary, shared_id)


def test_duplicate_source_with_different_hashes_fails_closed(
    v2_package: dict,
) -> None:
    package = copy.deepcopy(v2_package)
    condition = package["screening_evidence"]["s2_conditions"][0]
    evidence_id = condition["evidence_ids"][0]
    duplicate = copy.deepcopy(condition["sources"][0])
    duplicate["source_record_id"] = f"{duplicate['source_record_id']}:duplicate"
    duplicate["assertion_sha256"] = "0" * 64
    condition["sources"].append(duplicate)

    summary = build_kg07_evidence(package)
    _assert_s2_fail_closed(summary, evidence_id)


def test_identical_duplicate_source_fails_closed(v2_package: dict) -> None:
    package = copy.deepcopy(v2_package)
    condition = package["screening_evidence"]["s2_conditions"][0]
    evidence_id = condition["evidence_ids"][0]
    condition["sources"].append(copy.deepcopy(condition["sources"][0]))

    summary = build_kg07_evidence(package)
    _assert_s2_fail_closed(summary, evidence_id)


def test_unique_s2_sources_still_certify(v2_summary: dict) -> None:
    full = v2_summary["validator_bundle"]
    scoped = v2_summary["llm_validator_bundle"]
    assert full["s2_conditions"]["validation"] == {
        "valid": True,
        "certified_condition_count": 5,
        "invalid_evidence_ids": [],
    }
    assert len(full["s2_conditions"]["certified_conditions"]) == 5
    assert len([meta for meta in full["evidence_meta"].values() if meta["kind"] == "s2"]) == 5
    assert scoped["s2_conditions"]["available"] is True
    assert len(scoped["s2_conditions"]["conditions"]) == 5
    assert len([meta for meta in scoped["evidence_meta"].values() if meta["kind"] == "s2"]) == 5


def test_legacy_sections_are_skeleton_only(summary: dict) -> None:
    legacy = summary["legacy_sections"]
    assert set(LEGACY_SECTION_KEYS).issubset(legacy)
    for key in (
        "screening_basis",
        "known_targets",
        "associated_pathways",
        "independent_validation",
        "mechanism_hypotheses",
    ):
        assert legacy[key] == []
    assert legacy["insufficient_evidence"] == []


def test_missing_fields_are_marked_not_fabricated(package: dict) -> None:
    shallow = copy.deepcopy(package)
    shallow.pop("graph_build_id", None)
    shallow["candidate"] = {
        key: value
        for key, value in shallow["candidate"].items()
        if key not in {"primary_dose", "n_signatures"}
    }
    shallow["screening_evidence"]["exp006"] = {
        key: value
        for key, value in shallow["screening_evidence"]["exp006"].items()
        if key not in {"primary_dose", "n_signatures"}
    }
    for condition in shallow["screening_evidence"]["s2_conditions"]:
        condition["condition"].pop("n_signatures", None)
    summary = build_kg07_evidence(shallow)
    missing = set(summary["metadata"]["missing_fields"])
    assert "graph_build_id" in missing
    assert "signature_coverage.n_signatures" in missing
    assert "signature_coverage.primary_dose" in missing
    assert summary["graph_build_id"] == ""


def test_adapter_is_deterministic(package: dict) -> None:
    first = build_kg07_evidence(package)
    second = build_kg07_evidence(package)
    assert first["graph_build_id"] == second["graph_build_id"]
    assert first["metadata"]["validator_bundle_sha256"] == second[
        "metadata"
    ]["validator_bundle_sha256"]
    assert first["dimensions"] == second["dimensions"]
    assert first["legacy_sections"] == second["legacy_sections"]


def test_target_evidence_has_structured_target_genes(summary: dict) -> None:
    bundle = summary["validator_bundle"]
    target_evidence = [
        meta for meta in bundle["evidence_meta"].values()
        if meta.get("kind") == "target"
    ]
    assert target_evidence
    for meta in target_evidence:
        assert meta["target_genes"], meta["evidence_id"]
        assert meta["gene_ids"]
        assert meta["gene_symbols"]
        for gene in meta["target_genes"]:
            assert gene["gene_id"]
            assert gene["path_ids"]
            for path_id in gene["path_ids"]:
                path = bundle["path_meta"][path_id]
                assert path["route_type"] == "direct_target"
                assert path["target_gene_id"] == gene["gene_id"]


def test_scoped_evidence_closure_has_no_path_leakage(summary: dict) -> None:
    full = summary["validator_bundle"]
    scoped = summary["llm_validator_bundle"]
    assert full["scope"] == "full"
    assert full["coverage_eligible"] is True
    assert scoped["scope"] == "llm_scoped"
    assert scoped["coverage_eligible"] is False
    assert set(scoped["allowed_evidence_ids"]) == set(scoped["evidence_meta"])
    allowed_paths = set(scoped["allowed_path_ids"])
    assert allowed_paths == set(scoped["path_meta"])
    for meta in scoped["evidence_meta"].values():
        assert set(meta["path_ids"]) <= allowed_paths, meta["evidence_id"]
        for path_id in meta["path_ids"]:
            assert path_id in scoped["path_meta"]
        for gene in meta.get("target_genes", []):
            assert set(gene["path_ids"]) <= allowed_paths
    assert len(scoped["allowed_path_ids"]) < len(full["allowed_path_ids"])
    assert len(scoped["allowed_evidence_ids"]) < len(full["allowed_evidence_ids"])
    assert scoped["dropped_evidence_ids"]


def test_real_target_claim_passes_validator_gene_binding(summary: dict) -> None:
    bundle = summary["llm_validator_bundle"]
    abl1 = next(row for row in bundle["targets"] if row["symbol"] == "ABL1")
    path_id = abl1["path_ids"][0]
    # Use the ABL1-only target evidence; other evidence is shared across many
    # target genes and would require declaring all of them.
    evidence_id = next(
        evidence_id
        for evidence_id in abl1["evidence_ids"]
        if bundle["evidence_meta"][evidence_id].get("target_genes")
        and {
            gene["gene_id"]
            for gene in bundle["evidence_meta"][evidence_id]["target_genes"]
        } == {abl1["gene_id"]}
    )
    claim = {
        "text": "ABL1 是该药物的直接靶点。",
        "relation": "association",
        "evidence_ids": [evidence_id],
        "path_ids": [path_id],
        "genes": [{"gene_id": abl1["gene_id"], "symbol": "ABL1"}],
    }
    clean, issues, _audit = validate_explanation(
        {"target_mechanism": [claim]}, bundle
    )
    assert clean["target_mechanism"], issues
    assert not any("target gene entity is not bound" in issue.get("issue", "") for issue in issues)
    assert clean["target_mechanism"][0]["genes"] == [
        {"gene_id": abl1["gene_id"], "symbol": "ABL1"}
    ]


def test_scoped_counts_are_not_full_counts_and_coverage_is_explicit(summary: dict) -> None:
    full = summary["validator_bundle"]
    scoped = summary["llm_validator_bundle"]
    assert full["counts"]["s1_enrichment_paths"] == 131
    assert full["counts"]["unique_pathways"] == 99
    assert scoped["counts"]["s1_enrichment_paths"] != full["counts"]["s1_enrichment_paths"]
    assert scoped["counts"]["unique_pathways"] != full["counts"]["unique_pathways"]
    assert scoped["counts"]["unique_evidence"] != full["counts"]["unique_evidence"]
    assert backend_coverage(full)["unique_evidence_count"] == 258
    assert backend_coverage(scoped)["unique_evidence_count"] == scoped["counts"][
        "unique_evidence"
    ]


def test_historical_graph_build_can_be_rejected_by_expected_id(package: dict) -> None:
    with pytest.raises(ValueError, match="does not match the resolved production GraphBuild"):
        build_kg07_evidence(
            package,
            expected_graph_build_id="GRAPH_BUILD:current-production-build",
        )


def test_s1_direction_is_real_or_missing_never_inferred() -> None:
    if not REAL_PACKAGE.exists():
        pytest.skip(f"real serialized package not available: {REAL_PACKAGE}")
    real = json.loads(REAL_PACKAGE.read_text(encoding="utf-8"))
    summary = build_kg07_evidence(real)
    s1_paths = [
        meta
        for meta in summary["validator_bundle"]["path_meta"].values()
        if meta["route_type"] == "s1_deg_overlap"
    ]
    assert len(s1_paths) == 5
    assert all(meta["s1_direction"] in {"UP", "DOWN"} for meta in s1_paths)
    assert sum(
        1 for meta in summary["validator_bundle"]["evidence_meta"].values()
        if meta.get("s1_direction")
    ) > 0

    stripped = copy.deepcopy(real)
    for path in stripped["paths"]:
        if path.get("route_type") == "s1_deg_overlap":
            path.pop("disease_expression_assertions", None)
            for edge in path.get("edges", []):
                edge["analysis_assertions"] = []
    stripped_summary = build_kg07_evidence(stripped)
    stripped_s1 = [
        meta
        for meta in stripped_summary["validator_bundle"]["path_meta"].values()
        if meta["route_type"] == "s1_deg_overlap"
    ]
    assert all(meta["s1_direction"] is None for meta in stripped_s1)
