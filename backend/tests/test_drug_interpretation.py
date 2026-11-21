"""Unit tests for citation-type binding, coverage semantics, caching, and DeepSeek wiring."""
from __future__ import annotations

import copy
import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.interpretation import evidence as evidence_module
from app.interpretation import prompt as prompt_module
from app.interpretation import provider, service, validator
from app.interpretation import router as router_module
from app.interpretation.schemas import DrugInterpretationRequest


def make_package() -> dict:
    direct = {
        "route_type": "direct_target",
        "target_gene_id": "ENSG00000116478",
        "path_id": "path-target",
        "reported_actions": ["INHIBITOR"],
        "evidence_ids": ["ev-target"],
        "nodes": [
            {"id": "drug", "type": "Drug", "label": "MOCETINOSTAT"},
            {"id": "ENSG00000116478", "type": "Gene", "label": "HDAC1"},
        ],
        "edges": [
            {
                "evidence_id": "ev-target",
                "sources": [
                    {
                        "evidence_id": "ev-target",
                        "verified_in_postgres": True,
                        "source_release": "OptimusKG",
                        "source_record_id": "src-1",
                        "predicate": "INHIBITOR",
                    }
                ],
            }
        ],
        "disease_expression_assertions": [],
        "pathway_adjusted_p_values": [],
    }
    deg = {
        "route_type": "s1_deg_overlap",
        "target_gene_id": "ENSG00000116478",
        "path_id": "path-deg",
        "evidence_ids": ["ev-deg"],
        "nodes": [{"id": "ENSG00000116478", "type": "Gene", "label": "HDAC1"}],
        "edges": [{"evidence_id": "ev-deg", "sources": [{"evidence_id": "ev-deg", "verified_in_postgres": True, "predicate": "HAS_DEG"}]}],
        "disease_expression_assertions": [
            {"gene_symbol": "HDAC1", "logFC": 1.0965, "adj.P.Val": 0.000133, "t": 4.52, "direction": "UP"}
        ],
        "pathway_adjusted_p_values": [],
    }
    pathway = {
        "route_type": "s1_enrichment_overlap",
        "target_gene_id": "ENSG00000116478",
        "pathway_id": "GO:0016032",
        "pathway_label": "viral process",
        "path_id": "path-go",
        "evidence_ids": ["ev-go"],
        "nodes": [
            {"id": "ENSG00000116478", "type": "Gene", "label": "HDAC1"},
            {"id": "GO:0016032", "type": "PathwayOrGeneSet", "label": "viral process"},
        ],
        "edges": [{"evidence_id": "ev-go", "sources": [{"evidence_id": "ev-go", "verified_in_postgres": True, "predicate": "HAS_ENRICHED_GENE"}]}],
        "disease_expression_assertions": [],
        "pathway_adjusted_p_values": [0.01, 0.004],
    }
    return {
        "project_id": "proj",
        "graph_build_id": "GRAPH_BUILD:test",
        "package_sha256": "a" * 64,
        "candidate": {"compound_id": "CHEMBL1", "mapping_status": "exact", "drug_name": "MOCETINOSTAT"},
        "coverage": {
            "compound_mapped": True,
            "direct_target_path_count": 1,
            "public_pathway_path_count": 0,
            "s1_enrichment_overlap_path_count": 1,
            "s1_deg_overlap_path_count": 1,
            "unique_evidence_count": 3,
        },
        "screening_evidence": {
            "s2_conditions": [
                {
                    "condition_key": "MCF7|10 um|24 h",
                    "condition": {"cell_id": "MCF7", "dose": "10.0 um", "time": "24 h", "WTCS_median": -0.5, "n_signatures": 1},
                    "evidence_ids": ["ev-s2"],
                    "evidence_complete": True,
                }
            ]
        },
        "paths": [direct, deg, pathway],
        "evidence_gaps": ["no target-to-public-pathway route was found"],
    }


def make_bundle() -> dict:
    package = make_package()
    row = {
        "rank": "2",
        "pert_id": "BRD-K16485616",
        "drug_name": "mocetinostat",
        "WTCS_median": "-0.5059",
        "n_signatures": "1",
        "n_24h_dose_conditions": "6",
        "negative_dose_fraction": "0.6667",
        "dose": "10.0 um",
        "cell_id": "MCF7",
        "time": "24 h",
    }
    return evidence_module.assemble_bundle(
        top10_row=row,
        top10_sha256="b" * 64,
        package=package,
        s1_rows=({"gene_symbol": "HDAC1", "logFC": 1.0965, "P.Value": 1e-5, "adj.P.Val": 0.000133, "t": 4.52, "direction": "up"},),
        grade_row={"pert_id": "BRD-K16485616", "known_target_grade": "database association"},
        phase_f_row={"external_s1_median_rank": "26"},
        exp006_row={"rank": "99"},
        s1_sha256="c" * 64,
    )


def make_s2_only_bundle() -> dict:
    """PHA-767491-like evidence: six S2 conditions, no target and no pathway."""
    package = make_package()
    package["paths"] = []
    package["candidate"] = {"compound_id": None, "mapping_status": "unmapped", "drug_name": "PHA-767491"}
    package["coverage"] = {
        "compound_mapped": False,
        "direct_target_path_count": 0,
        "public_pathway_path_count": 0,
        "s1_enrichment_overlap_path_count": 0,
        "s1_deg_overlap_path_count": 0,
        "unique_evidence_count": 1,
    }
    return evidence_module.assemble_bundle(
        top10_row={"rank": "6", "pert_id": "BRD-K50000283", "drug_name": "PHA-767491", "WTCS_median": "-0.4564", "n_signatures": "1", "n_24h_dose_conditions": "6"},
        top10_sha256="b" * 64,
        package=package,
        s1_rows=None,
        grade_row={"pert_id": "BRD-K50000283"},
        phase_f_row=None,
        exp006_row=None,
        s1_sha256="c" * 64,
    )


def make_claim(text, evidence_ids=(), path_ids=(), relation="association", layer="A"):
    return {"text": text, "evidence_ids": list(evidence_ids), "path_ids": list(path_ids), "relation": relation, "layer": layer}


def make_two_target_bundle() -> dict:
    """Two independent drug-target paths for entity-binding tests."""

    def target_path(path_id: str, evidence_id: str, gene_id: str, symbol: str) -> dict:
        return {
            "route_type": "direct_target",
            "target_gene_id": gene_id,
            "path_id": path_id,
            "reported_actions": ["INHIBITOR"],
            "evidence_ids": [evidence_id],
            "nodes": [
                {"id": "drug", "type": "Drug", "label": "TEST-DRUG"},
                {"id": gene_id, "type": "Gene", "label": symbol},
            ],
            "edges": [
                {
                    "evidence_id": evidence_id,
                    "sources": [
                        {
                            "evidence_id": evidence_id,
                            "verified_in_postgres": True,
                            "source_release": "OptimusKG",
                            "source_record_id": f"src-{symbol}",
                            "predicate": "INHIBITOR",
                        }
                    ],
                }
            ],
            "disease_expression_assertions": [],
            "pathway_adjusted_p_values": [],
        }

    package = {
        "project_id": "proj",
        "graph_build_id": "GRAPH_BUILD:test",
        "package_sha256": "a" * 64,
        "candidate": {"compound_id": "CHEMBL1", "mapping_status": "exact", "drug_name": "TEST-DRUG"},
        "coverage": {
            "compound_mapped": True,
            "direct_target_path_count": 2,
            "public_pathway_path_count": 0,
            "s1_enrichment_overlap_path_count": 0,
            "s1_deg_overlap_path_count": 0,
            "unique_evidence_count": 2,
        },
        "screening_evidence": {"s2_conditions": []},
        "paths": [
            target_path("path-egfr", "ev-egfr", "ENSG00000146648", "EGFR"),
            target_path("path-abl1", "ev-abl1", "ENSG00000097007", "ABL1"),
        ],
        "evidence_gaps": [],
    }
    return evidence_module.assemble_bundle(
        top10_row={"rank": "1", "pert_id": "BRD-TEST", "drug_name": "TEST-DRUG", "WTCS_median": "-0.5"},
        top10_sha256="b" * 64,
        package=package,
        s1_rows=None,
        grade_row=None,
        phase_f_row=None,
        exp006_row=None,
        s1_sha256="c" * 64,
    )


def make_two_target_claim(text, evidence_ids=(), path_ids=(), genes=None, relation="inhibition"):
    claim = make_claim(text, evidence_ids, path_ids, relation=relation)
    if genes is not None:
        claim["genes"] = genes
    return claim


def test_frozen_top10_hash_and_rank_authority():
    rows, sha = evidence_module.load_frozen_top10()
    assert len(rows) == 10
    assert len(sha) == 64
    row = evidence_module.find_top10_row("BRD-K44227013", rows)
    assert row["drug_name"] == "ponatinib"
    assert row["rank"] == "1"


def test_assemble_bundle_keeps_frozen_rank_and_s1_stats():
    bundle = make_bundle()
    assert bundle["candidate"]["rank"] == 2
    assert bundle["candidate"]["drug_id"] == "BRD-K16485616"
    assert bundle["targets"][0]["symbol"] == "HDAC1"
    assert bundle["targets"][0]["relation"] == "inhibition"
    assert bundle["targets"][0]["s1_deg"]["logFC"] == pytest.approx(1.0965)
    assert bundle["targets"][0]["s1_deg"]["p_value"] == pytest.approx(1e-5)
    assert bundle["targets"][0]["s1_deg"]["fdr"] == pytest.approx(0.000133)
    assert bundle["pathways"][0]["adjusted_p"] == pytest.approx(0.004)
    assert "ev-target" in bundle["allowed_evidence_ids"]
    assert "ev-s2" in bundle["allowed_evidence_ids"]
    assert bundle["rank_evidence_id"] in bundle["allowed_evidence_ids"]
    assert "path-target" in bundle["allowed_path_ids"]
    # The synthetic rank reference must not inflate the real evidence count.
    assert bundle["counts"]["unique_evidence"] == 4


def test_target_without_deg_is_not_marked_as_disease_gene():
    package = make_package()
    package["paths"] = [item for item in package["paths"] if item["route_type"] != "s1_deg_overlap"]
    bundle = evidence_module.assemble_bundle(
        top10_row={"rank": "3", "pert_id": "X", "drug_name": "X", "WTCS_median": "-0.1"},
        top10_sha256="b" * 64,
        package=package,
        s1_rows=None,
        grade_row=None,
        phase_f_row=None,
        exp006_row=None,
    )
    assert bundle["targets"][0]["s1_deg"] is None


def test_abl1_evidence_cannot_support_egfr_claim():
    bundle = make_two_target_bundle()
    explanation = {
        "screening_rationale": [],
        "target_mechanism": [
            make_two_target_claim("EGFR 是药物靶点", ["ev-abl1"], ["path-abl1"], genes=[{"symbol": "EGFR"}])
        ],
        "pathway_association": [],
        "er_positive_relevance": [],
        "mechanism_hypotheses": [],
    }
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"] == []
    assert any("target gene entity is not bound" in item["issue"] for item in issues)


def test_egfr_evidence_cannot_support_abl1_claim():
    bundle = make_two_target_bundle()
    explanation = {
        "screening_rationale": [],
        "target_mechanism": [
            make_two_target_claim("ABL1 是药物靶点", ["ev-egfr"], ["path-egfr"], genes=[{"symbol": "ABL1"}])
        ],
        "pathway_association": [],
        "er_positive_relevance": [],
        "mechanism_hypotheses": [],
    }
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"] == []
    assert any("target gene entity is not bound" in item["issue"] for item in issues)


def test_multiple_target_evidence_and_entities_match():
    bundle = make_two_target_bundle()
    explanation = {
        "screening_rationale": [],
        "target_mechanism": [
            make_two_target_claim(
                "EGFR 与 ABL1 均为药物靶点",
                ["ev-egfr", "ev-abl1"],
                ["path-egfr", "path-abl1"],
                genes=[{"gene_id": "ENSG00000146648", "symbol": "EGFR"}, {"gene_id": "ENSG00000097007", "symbol": "ABL1"}],
            )
        ],
        "pathway_association": [],
        "er_positive_relevance": [],
        "mechanism_hypotheses": [],
    }
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert len(clean["target_mechanism"]) == 1
    assert {item["symbol"] for item in clean["target_mechanism"][0]["genes"]} == {"EGFR", "ABL1"}


def test_multiple_target_evidence_and_entities_cross_match_is_rejected():
    bundle = make_two_target_bundle()
    explanation = {
        "screening_rationale": [],
        "target_mechanism": [
            make_two_target_claim(
                "EGFR 与 ABL1 均为药物靶点",
                ["ev-egfr"],
                ["path-egfr"],
                genes=[{"gene_id": "ENSG00000146648", "symbol": "EGFR"}, {"gene_id": "ENSG00000097007", "symbol": "ABL1"}],
            )
        ],
        "pathway_association": [],
        "er_positive_relevance": [],
        "mechanism_hypotheses": [],
    }
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"] == []
    assert any("target gene entity is not bound" in item["issue"] for item in issues)


def test_declared_entity_with_extra_other_gene_evidence_is_rejected():
    bundle = make_two_target_bundle()
    explanation = {
        "screening_rationale": [],
        "target_mechanism": [
            make_two_target_claim("EGFR 是药物靶点", ["ev-egfr", "ev-abl1"], ["path-egfr", "path-abl1"], genes=[{"symbol": "EGFR"}])
        ],
        "pathway_association": [],
        "er_positive_relevance": [],
        "mechanism_hypotheses": [],
    }
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"] == []
    assert any("target gene entity is not bound" in item["issue"] for item in issues)


def test_gene_alias_is_normalized_exactly():
    bundle = make_two_target_bundle()
    explanation = {
        "screening_rationale": [],
        "target_mechanism": [make_two_target_claim("EGFR 是药物靶点", ["ev-egfr"], ["path-egfr"], genes=[{"symbol": "ERBB1"}])],
        "pathway_association": [],
        "er_positive_relevance": [],
        "mechanism_hypotheses": [],
    }
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert len(clean["target_mechanism"]) == 1
    assert clean["target_mechanism"][0]["genes"] == [{"gene_id": "ENSG00000146648", "symbol": "EGFR"}]


def test_unknown_alias_cannot_pass_as_fact():
    bundle = make_two_target_bundle()
    explanation = {
        "screening_rationale": [],
        "target_mechanism": [make_two_target_claim("EGFRvIII 是药物靶点", ["ev-egfr"], ["path-egfr"], genes=[{"symbol": "EGFRvIII"}])],
        "pathway_association": [],
        "er_positive_relevance": [],
        "mechanism_hypotheses": [],
    }
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"] == []
    assert any("unknown or invalid gene entity" in item["issue"] for item in issues)


def test_target_claim_without_gene_entity_is_rejected():
    bundle = make_two_target_bundle()
    explanation = {
        "screening_rationale": [],
        "target_mechanism": [make_claim("该药抑制一个激酶靶点", ["ev-egfr"], ["path-egfr"], relation="inhibition")],
        "pathway_association": [],
        "er_positive_relevance": [],
        "mechanism_hypotheses": [],
    }
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"] == []
    assert any("target gene entity is not bound" in item["issue"] for item in issues)


def test_text_only_exact_symbol_keeps_backward_compatibility():
    bundle = make_two_target_bundle()
    explanation = {
        "screening_rationale": [],
        "target_mechanism": [make_claim("EGFR 是药物靶点", ["ev-egfr"], ["path-egfr"], relation="inhibition")],
        "pathway_association": [],
        "er_positive_relevance": [],
        "mechanism_hypotheses": [],
    }
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert len(clean["target_mechanism"]) == 1
    assert clean["target_mechanism"][0]["genes"] == [{"gene_id": "ENSG00000146648", "symbol": "EGFR"}]


def test_text_gene_not_declared_in_genes_is_rejected():
    bundle = make_two_target_bundle()
    explanation = {
        "screening_rationale": [],
        "target_mechanism": [make_two_target_claim("ABL1 是药物靶点", ["ev-egfr"], ["path-egfr"], genes=[{"symbol": "EGFR"}])],
        "pathway_association": [],
        "er_positive_relevance": [],
        "mechanism_hypotheses": [],
    }
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"] == []
    assert any("text names a gene entity not declared" in item["issue"] for item in issues)


def test_prompt_contains_scientific_constraints():
    messages = prompt_module.build_messages(make_bundle())
    system = messages[0]["content"]
    for token in ("不得虚构", "引用类型必须与段落匹配", "rank_evidence_id", "path_ids", "A|B|C", "靶点实体绑定", "genes", "不要输出 limitations"):
        assert token in system
    payload = json.loads(messages[1]["content"])
    assert payload["rank_source"].startswith("experiments/final_results/results/final_top10.csv")
    assert payload["rank_evidence_id"].startswith("TOP10:")


def test_pha_s2_evidence_cannot_support_target_mechanism():
    bundle = make_s2_only_bundle()
    explanation = {
        "screening_rationale": [make_claim("排名第六且 WTCS 为负", ["ev-s2", bundle["rank_evidence_id"]])],
        "target_mechanism": [make_claim("PHA-767491 抑制 CDK1", ["ev-s2"], relation="inhibition")],
        "pathway_association": [],
        "er_positive_relevance": [],
        "mechanism_hypotheses": [],
    }
    clean, issues, audit = validator.validate_explanation(explanation, bundle)
    assert clean["screening_rationale"]
    assert clean["target_mechanism"] == []
    assert any(item["section"] == "target_mechanism" and "citation type" in item["issue"] for item in issues)


def test_s2_evidence_id_cannot_pass_target_mechanism():
    bundle = make_bundle()
    explanation = {"screening_rationale": [], "target_mechanism": [make_claim("HDAC1 是靶点", ["ev-s2"])], "pathway_association": [], "er_positive_relevance": [], "mechanism_hypotheses": []}
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"] == []


def test_target_evidence_cannot_pass_pathway_association():
    bundle = make_bundle()
    explanation = {"screening_rationale": [], "target_mechanism": [], "pathway_association": [make_claim("HDAC1 参与 viral process", ["ev-target"], ["path-target"])], "er_positive_relevance": [], "mechanism_hypotheses": []}
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["pathway_association"] == []


def test_foreign_or_unknown_evidence_id_is_rejected():
    bundle = make_bundle()
    explanation = {"screening_rationale": [], "target_mechanism": [make_claim("HDAC1 是靶点", ["ev-other-drug"], ["path-target"])], "pathway_association": [], "er_positive_relevance": [], "mechanism_hypotheses": []}
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"] == []
    assert any("unknown evidence_ids" in item["issue"] for item in issues)
    assert any("not in the current evidence bundle" in item["issue"] for item in issues)


def test_nonexistent_path_id_is_rejected():
    bundle = make_bundle()
    explanation = {"screening_rationale": [], "target_mechanism": [make_claim("HDAC1 是靶点", ["ev-target"], ["path-ghost"])], "pathway_association": [], "er_positive_relevance": [], "mechanism_hypotheses": []}
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"] == []
    assert any("unknown path_ids" in item["issue"] for item in issues)
    assert any("not in the current evidence bundle" in item["issue"] for item in issues)


def test_empty_citation_claim_is_deleted():
    bundle = make_bundle()
    explanation = {"screening_rationale": [], "target_mechanism": [make_claim("HDAC1 是靶点", [], [])], "pathway_association": [], "er_positive_relevance": [], "mechanism_hypotheses": []}
    clean, _, audit = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"] == []
    assert audit["dropped_claims"] == 1


def test_limitations_mechanism_assertion_is_not_trusted(monkeypatch, tmp_path):
    bundle = make_bundle()
    monkeypatch.setattr(evidence_module, "collect_evidence", lambda *args, **kwargs: bundle)
    monkeypatch.setattr(service, "CACHE_DIR", tmp_path)

    def fake_chat(messages):
        return (
            {
                "screening_rationale": [make_claim("排名第二", ["ev-s2"], [])],
                "target_mechanism": [make_claim("HDAC1 有靶点断言", ["ev-target"], ["path-target"], relation="inhibition")],
                "pathway_association": [],
                "er_positive_relevance": [],
                "mechanism_hypotheses": [],
                "limitations": ["HDAC1 是已证实的治疗靶点"],
                "validation_suggestions": ["可直接用于临床"],
                "insufficient_evidence": [],
                "evidence_assessment": {"verdict": "sufficient", "basis": "非常充分"},
            },
            {"model": "deepseek-flash", "latency_seconds": 0.1, "usage": {}},
        )

    monkeypatch.setattr(provider, "chat_json", fake_chat)
    result = service.interpret_drug(None, "proj", "BRD-K16485616")
    assert "HDAC1 是已证实的治疗靶点" not in json.dumps(result["limitations"], ensure_ascii=False)
    assert "可直接用于临床" not in json.dumps(result["validation_suggestions"], ensure_ascii=False)
    assert result["claim_audit"]["ignored_model_free_text"]
    assert "model_basis" not in result["evidence_assessment"]


def test_causal_relation_is_downgraded():
    bundle = make_bundle()
    explanation = {"screening_rationale": [], "target_mechanism": [make_claim("HDAC1 导致转录改变", ["ev-target"], ["path-target"], relation="causal")], "pathway_association": [], "er_positive_relevance": [], "mechanism_hypotheses": []}
    clean, issues, audit = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"][0]["relation"] == "association"
    assert clean["target_mechanism"][0]["text"].startswith("待验证关联（因果表述已降级）")
    assert audit["downgraded_claims"] >= 1


def test_efficacy_claim_is_dropped():
    bundle = make_bundle()
    explanation = {"screening_rationale": [], "target_mechanism": [make_claim("HDAC1 可用于治疗乳腺癌", ["ev-target"], ["path-target"])], "pathway_association": [], "er_positive_relevance": [], "mechanism_hypotheses": []}
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"] == []
    assert any("efficacy" in item["issue"] for item in issues)


def test_expression_direction_contradiction_is_dropped():
    bundle = make_bundle()
    explanation = {"screening_rationale": [], "target_mechanism": [make_claim("HDAC1 在 S1 中下调", ["ev-target", "ev-deg"], ["path-target", "path-deg"], relation="inhibition")], "pathway_association": [], "er_positive_relevance": [], "mechanism_hypotheses": []}
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"] == []
    assert any("contradicts" in item["issue"] for item in issues)


def test_drug_expression_effect_without_s1_context_is_dropped():
    bundle = make_bundle()
    explanation = {"screening_rationale": [], "target_mechanism": [make_claim("该药上调 HDAC1", ["ev-target", "ev-deg"], ["path-target", "path-deg"])], "pathway_association": [], "er_positive_relevance": [], "mechanism_hypotheses": []}
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["target_mechanism"] == []
    assert any("expression direction" in item["issue"] for item in issues)


def test_hypothesis_is_forced_to_layer_c():
    bundle = make_bundle()
    explanation = {
        "screening_rationale": [], "target_mechanism": [], "pathway_association": [],
        "er_positive_relevance": [],
        "mechanism_hypotheses": [make_claim("HDAC 抑制可能影响病毒应答通路", ["ev-go"], ["path-go"])],
    }
    clean, _, _ = validator.validate_explanation(explanation, bundle)
    assert clean["mechanism_hypotheses"][0]["layer"] == "C"
    assert clean["mechanism_hypotheses"][0]["text"].startswith("待验证假设")


def test_screening_reversal_term_is_allowed():
    bundle = make_bundle()
    explanation = {"screening_rationale": [make_claim("排名第二且负 WTCS 提示表达逆转", ["ev-s2"])], "target_mechanism": [], "pathway_association": [], "er_positive_relevance": [], "mechanism_hypotheses": []}
    clean, issues, _ = validator.validate_explanation(explanation, bundle)
    assert clean["screening_rationale"]
    assert clean["screening_rationale"][0]["layer"] == "A"


def test_coverage_levels_are_not_scientific_grades():
    bundle = make_bundle()
    coverage = validator.backend_coverage(bundle)
    assert coverage["coverage_level"] in {"coverage_high", "coverage_limited"}
    assert "not" not in coverage["coverage_level"]
    assert "verdict" not in coverage
    empty = {**bundle, "compound": {"mapped": False}, "counts": {"direct_target_paths": 0, "public_pathway_paths": 0, "s1_enrichment_paths": 0, "unique_evidence": 0}}
    assert validator.backend_coverage(empty)["coverage_level"] == "coverage_insufficient"


def test_provider_requires_key(monkeypatch):
    monkeypatch.setattr(provider.settings, "deepseek_api_key", "")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(provider.ProviderConfigError):
        provider.chat_json([{"role": "user", "content": "x"}])


def test_provider_parses_fenced_json():
    parsed = provider.parse_content('```json\n{"a": 1}\n```')
    assert parsed == {"a": 1}
    with pytest.raises(provider.ProviderResponseError):
        provider.parse_content("<html>not json</html>")


def test_service_caches_and_keeps_key_out_of_output(monkeypatch, tmp_path):
    bundle = make_bundle()
    monkeypatch.setattr(evidence_module, "collect_evidence", lambda *args, **kwargs: bundle)
    monkeypatch.setattr(service, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(provider.settings, "deepseek_api_key", "super-secret-key")
    calls = []

    def fake_chat(messages):
        calls.append(messages)
        content = {
            "screening_rationale": [make_claim("排名第二，WTCS 为负表示反向关联", ["ev-s2"], [])],
            "target_mechanism": [make_claim("HDAC1 有靶点断言并命中 S1 差异表达", ["ev-target", "ev-deg"], ["path-target", "path-deg"], relation="inhibition")],
            "pathway_association": [make_claim("与 viral process 存在交汇", ["ev-go"], ["path-go"])],
            "er_positive_relevance": [make_claim("该通路与 ER+ 项目表达背景存在联系", ["ev-go"], ["path-go"])],
            "mechanism_hypotheses": [make_claim("可能存在 HDAC 相关的转录调控假设", ["ev-target"], ["path-target"])],
        }
        return content, {"model": "deepseek-flash", "latency_seconds": 0.1, "usage": {"total_tokens": 10}}

    monkeypatch.setattr(provider, "chat_json", fake_chat)
    first = service.interpret_drug(None, "proj", "BRD-K16485616")
    second = service.interpret_drug(None, "proj", "BRD-K16485616")
    assert first["model_name"] == "deepseek-flash"
    assert first["rank"] == 2
    assert first["evidence_assessment"]["coverage_level"] == "coverage_limited"
    assert first["cached"] is False
    assert second["cached"] is True
    assert len(calls) == 1
    assert "super-secret-key" not in json.dumps(first, ensure_ascii=False)
    cache_file = tmp_path / f"{service.cache_key(bundle)}.json"
    assert cache_file.is_file()


def test_service_skips_model_when_no_evidence(monkeypatch, tmp_path):
    bundle = make_bundle()
    bundle["counts"] = {"s2_conditions": 0, "direct_target_paths": 0, "public_pathway_paths": 0, "s1_enrichment_paths": 0, "s1_deg_paths": 0, "unique_evidence": 0}
    bundle["compound"] = {"mapped": False, "compound_id": None}
    bundle["targets"] = []
    bundle["pathways"] = []
    bundle["s2_conditions"] = []
    monkeypatch.setattr(evidence_module, "collect_evidence", lambda *args, **kwargs: bundle)
    monkeypatch.setattr(service, "CACHE_DIR", tmp_path)

    def boom(*args, **kwargs):
        raise AssertionError("model must not be called without evidence")

    monkeypatch.setattr(provider, "chat_json", boom)
    result = service.interpret_drug(None, "proj", "BRD-K50000283")
    assert result["status"] == "coverage_insufficient"
    assert result["evidence_assessment"]["coverage_level"] == "coverage_insufficient"
    assert result["section_status"]["target_mechanism"] == "no_target_evidence"
    assert result["section_status"]["pathway_association"] == "no_pathway_evidence"
    assert result["mode"] == "deterministic_rule"


def test_s1_hash_changes_cache_key():
    base = make_bundle()
    changed = {**base, "s1_sha256": "d" * 64}
    assert service.cache_key(base) != service.cache_key(changed)


def test_prompt_text_changes_cache_key(monkeypatch):
    bundle = make_bundle()
    original = service.cache_key(bundle)
    monkeypatch.setattr(prompt_module, "SYSTEM_PROMPT", prompt_module.SYSTEM_PROMPT + "\n额外约束。")
    assert service.cache_key(bundle) != original


@pytest.mark.parametrize("status_code", [401, 403])
def test_provider_auth_status_maps_to_configuration_error(monkeypatch, status_code):
    monkeypatch.setattr(provider.settings, "deepseek_api_key", "super-secret-key")

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status_code, "Forbidden", {}, io.BytesIO(b'{"error":"denied"}'))

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(provider.ProviderAuthError) as error:
        provider._post_once({"model": "deepseek-flash"}, "super-secret-key")
    assert f"HTTP {status_code}" in str(error.value)
    assert "super-secret-key" not in str(error.value)


def test_router_maps_provider_auth_error_to_friendly_503(monkeypatch):
    def denied(*args, **kwargs):
        raise provider.ProviderAuthError("DeepSeek 拒绝认证（HTTP 403）")

    monkeypatch.setattr(service, "interpret_drug", denied)
    with pytest.raises(HTTPException) as error:
        router_module.create_drug_interpretation(
            "proj",
            DrugInterpretationRequest(drug_id="BRD-K44227013"),
            None,
        )
    assert error.value.status_code == 503
    assert "认证失败" in str(error.value.detail)
    assert "403" not in str(error.value.detail)


def test_semantic_cache_ignores_timestamp_derived_hashes():
    base = make_bundle()
    changed = copy.deepcopy(base)
    changed["package_sha256"] = "d" * 64
    changed["evidence_input_sha256"] = "e" * 64

    assert evidence_module.semantic_evidence_sha256(base) == evidence_module.semantic_evidence_sha256(changed)
    assert service.cache_key(base) == service.cache_key(changed)


def test_semantic_cache_ignores_unordered_list_order():
    base = make_two_target_bundle()
    changed = copy.deepcopy(base)
    changed["targets"] = list(reversed(changed["targets"]))
    changed["evidence_references"] = list(reversed(changed["evidence_references"]))
    changed["allowed_evidence_ids"] = list(reversed(changed["allowed_evidence_ids"]))
    for target in changed["targets"]:
        target["evidence_ids"] = list(reversed(target["evidence_ids"]))
        target["path_ids"] = list(reversed(target["path_ids"]))

    assert evidence_module.semantic_evidence_sha256(base) == evidence_module.semantic_evidence_sha256(changed)
    assert service.cache_key(base) == service.cache_key(changed)


def test_semantic_cache_preserves_ordered_pathway_list():
    base = make_bundle()
    first = copy.deepcopy(base["pathways"][0])
    second = copy.deepcopy(first)
    second["pathway_id"] = "GO:0000002"
    second["label"] = "second pathway"
    second["path_ids"] = ["path-go-2"]
    base["pathways"] = [first, second]
    changed = copy.deepcopy(base)
    changed["pathways"] = list(reversed(changed["pathways"]))

    assert service.cache_key(base) != service.cache_key(changed)


def test_semantic_cache_detects_reference_change():
    base = make_bundle()
    changed = copy.deepcopy(base)
    changed["evidence_references"][0]["source_record_id"] = "changed-source-record"

    assert service.cache_key(base) != service.cache_key(changed)


def test_semantic_cache_detects_target_gene_change():
    base = make_two_target_bundle()
    changed = copy.deepcopy(base)
    changed["targets"][0]["symbol"] = "ABL2"
    changed["targets"][0]["gene_id"] = "ENSG00000097008"

    assert service.cache_key(base) != service.cache_key(changed)


def test_semantic_cache_detects_pathway_change():
    base = make_bundle()
    changed = copy.deepcopy(base)
    changed["pathways"][0]["pathway_id"] = "GO:0000003"

    assert service.cache_key(base) != service.cache_key(changed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("top10_sha256", "1" * 64),
        ("s1_sha256", "2" * 64),
        ("graph_build_id", "GRAPH_BUILD:changed"),
    ],
)
def test_semantic_cache_detects_source_identity_change(field, value):
    base = make_bundle()
    changed = copy.deepcopy(base)
    changed[field] = value

    assert service.cache_key(base) != service.cache_key(changed)


def test_cache_key_detects_prompt_version_change(monkeypatch):
    base = make_bundle()
    original = service.cache_key(base)
    monkeypatch.setattr(prompt_module, "PROMPT_VERSION", "drug-interpretation-test")
    assert service.cache_key(base) != original


def test_cache_key_detects_model_change(monkeypatch):
    base = make_bundle()
    original = service.cache_key(base)
    monkeypatch.setattr(service.settings, "deepseek_model", "another-model")
    assert service.cache_key(base) != original


def test_service_cache_hits_across_kg06_timestamp_hash_changes(monkeypatch, tmp_path):
    first_bundle = make_bundle()
    second_bundle = copy.deepcopy(first_bundle)
    second_bundle["package_sha256"] = "f" * 64
    second_bundle["evidence_input_sha256"] = "9" * 64
    bundles = iter((first_bundle, second_bundle))

    monkeypatch.setattr(evidence_module, "collect_evidence", lambda *args, **kwargs: next(bundles))
    monkeypatch.setattr(service, "CACHE_DIR", tmp_path)
    calls = []

    def fake_chat(messages):
        calls.append(messages)
        return {
            "screening_rationale": [make_claim("排名第二，WTCS 为负表示反向关联", ["ev-s2"], [])],
            "target_mechanism": [make_claim("HDAC1 有靶点断言", ["ev-target"], ["path-target"], relation="inhibition")],
            "pathway_association": [make_claim("与 viral process 存在交汇", ["ev-go"], ["path-go"])],
            "er_positive_relevance": [make_claim("与 ER+ 表达背景存在联系", ["ev-go"], ["path-go"])],
            "mechanism_hypotheses": [make_claim("可能存在 HDAC 相关假设", ["ev-target"], ["path-target"])],
        }, {"model": "deepseek-flash", "latency_seconds": 0.1, "usage": {"total_tokens": 10}}

    monkeypatch.setattr(provider, "chat_json", fake_chat)
    first = service.interpret_drug(None, "proj", "BRD-K16485616")
    second = service.interpret_drug(None, "proj", "BRD-K16485616")

    assert first_bundle["evidence_input_sha256"] != second_bundle["evidence_input_sha256"]
    assert first["cached"] is False
    assert second["cached"] is True
    assert len(calls) == 1
    assert second["evidence_input_sha256"] == first["evidence_input_sha256"]


# --- Stage 2A-4.1.1: Phase F GraphBuild consistency for the Top10 AI input ---

CURRENT_BUILD = "GRAPH_BUILD:test"  # make_package() uses this build ID
OLD_BUILD = "GRAPH_BUILD:old-manifest-build"

PHASE_F_KG_KEYS = (
    "kg_mapping_status",
    "kg_supported_route_count",
    "kg_unique_target_gene_count",
    "kg_unique_pathway_count",
)


def make_phase_f_row(with_kg: bool = True) -> dict:
    row = {
        "rank": "2",
        "external_s1_median_rank": "26",
        "external_s1_conditions": "3",
        "erpos_mean_sensitivity": "0.4",
        "erpos_median_sensitivity": "0.35",
        "n_erpos_lines": "10",
        "erneg_mean_sensitivity": "0.7",
        "n_erneg_lines": "6",
        "mcf7_sensitivity": "0.5",
        "stability_grade": "B",
    }
    if with_kg:
        row.update(
            {
                "kg_mapping_status": "exact",
                "kg_supported_route_count": "131",
                "kg_unique_target_gene_count": "9",
                "kg_unique_pathway_count": "40",
            }
        )
    return row


def make_phase_f_provenance(state: str) -> dict:
    base = {
        "manifest_sha256": "a" * 64,
        "data_sha256": "b" * 64,
        "source": "experiments/phase_f_integrated_evidence/candidate_evidence_with_kg.csv",
        "expected_graph_build_id": CURRENT_BUILD,
    }
    if state == "match":
        return {
            **base,
            "available": True,
            "kg_fields_available": True,
            "graph_build_matches": True,
            "manifest_graph_build_id": CURRENT_BUILD,
        }
    if state == "mismatch":
        return {
            **base,
            "available": False,
            "kg_fields_available": False,
            "graph_build_matches": False,
            "reason": "graph_build_mismatch",
            "manifest_graph_build_id": OLD_BUILD,
            "independent_fields_retained": True,
            "kg_derived_fields_isolated": True,
        }
    if state == "missing_id":
        return {
            **base,
            "available": False,
            "kg_fields_available": False,
            "graph_build_matches": False,
            "reason": "missing_graph_build_id",
            "manifest_graph_build_id": None,
            "independent_fields_retained": True,
            "kg_derived_fields_isolated": True,
        }
    if state == "hash_mismatch":
        return {
            **base,
            "available": False,
            "kg_fields_available": False,
            "graph_build_matches": True,
            "reason": "hash_mismatch",
            "manifest_graph_build_id": CURRENT_BUILD,
        }
    raise ValueError(state)


def assemble_with_phase_f(row, provenance) -> dict:
    return evidence_module.assemble_bundle(
        top10_row={"rank": "2", "pert_id": "BRD-K16485616", "drug_name": "mocetinostat", "WTCS_median": "-0.5059"},
        top10_sha256="b" * 64,
        package=make_package(),
        s1_rows=None,
        grade_row=None,
        phase_f_row=row,
        exp006_row=None,
        s1_sha256="c" * 64,
        phase_f_provenance=provenance,
    )


def test_phase_f_matching_build_keeps_kg_stats_and_independent_fields():
    bundle = assemble_with_phase_f(make_phase_f_row(), make_phase_f_provenance("match"))
    phase_f = bundle["phase_f"]
    assert phase_f["kg_supported_route_count"] == "131"
    assert phase_f["kg_unique_target_gene_count"] == "9"
    assert phase_f["kg_unique_pathway_count"] == "40"
    assert phase_f["kg_mapping_status"] == "exact"
    assert phase_f["external_s1_median_rank"] == "26"
    assert phase_f["stability_grade"] == "B"
    assert bundle["phase_f_provenance"]["graph_build_matches"] is True
    # 与修复前白名单完全一致：匹配时行为不变
    assert set(phase_f) == set(make_phase_f_row())


def test_phase_f_mismatched_build_excludes_kg_stats_from_ai_input():
    safe_row = make_phase_f_row(with_kg=False)  # kg06 隔离后的行
    bundle = assemble_with_phase_f(safe_row, make_phase_f_provenance("mismatch"))
    phase_f = bundle["phase_f"]
    assert all(key not in phase_f for key in PHASE_F_KG_KEYS)
    assert phase_f["external_s1_median_rank"] == "26"
    assert phase_f["erpos_mean_sensitivity"] == "0.4"
    assert phase_f["stability_grade"] == "B"
    # provenance 不被丢弃，隔离状态可审计
    assert bundle["phase_f_provenance"]["reason"] == "graph_build_mismatch"
    assert bundle["phase_f_provenance"]["manifest_graph_build_id"] == OLD_BUILD


def test_phase_f_mismatch_strips_kg_stats_even_if_row_still_carries_them():
    # 防御纵深：即使上游行未剥离 kg 字段，组装门控也必须拦截
    bundle = assemble_with_phase_f(make_phase_f_row(), make_phase_f_provenance("mismatch"))
    assert all(key not in bundle["phase_f"] for key in PHASE_F_KG_KEYS)
    assert bundle["phase_f"]["external_s1_median_rank"] == "26"


def test_phase_f_missing_manifest_build_id_excludes_kg_stats():
    bundle = assemble_with_phase_f(make_phase_f_row(), make_phase_f_provenance("missing_id"))
    assert all(key not in bundle["phase_f"] for key in PHASE_F_KG_KEYS)
    assert bundle["phase_f"]["external_s1_median_rank"] == "26"
    assert bundle["phase_f_provenance"]["reason"] == "missing_graph_build_id"


def test_phase_f_without_provenance_fails_closed():
    bundle = assemble_with_phase_f(make_phase_f_row(), None)
    assert all(key not in bundle["phase_f"] for key in PHASE_F_KG_KEYS)
    assert bundle["phase_f"]["external_s1_median_rank"] == "26"


def test_phase_f_provenance_must_match_package_build_at_assembly_time():
    # provenance 声称可用，但 manifest build 与包 build 不同 → 仍然拦截
    forged = make_phase_f_provenance("match")
    forged["manifest_graph_build_id"] = OLD_BUILD
    bundle = assemble_with_phase_f(make_phase_f_row(), forged)
    assert all(key not in bundle["phase_f"] for key in PHASE_F_KG_KEYS)


def test_phase_f_hash_mismatch_stays_fail_closed():
    bundle = assemble_with_phase_f(None, make_phase_f_provenance("hash_mismatch"))
    assert bundle["phase_f"] is None
    assert bundle["phase_f_provenance"]["reason"] == "hash_mismatch"


def test_semantic_hash_separates_matching_and_mismatched_phase_f():
    matched = assemble_with_phase_f(make_phase_f_row(), make_phase_f_provenance("match"))
    mismatched = assemble_with_phase_f(make_phase_f_row(), make_phase_f_provenance("mismatch"))
    assert evidence_module.semantic_evidence_sha256(matched) != evidence_module.semantic_evidence_sha256(mismatched)
    assert service.cache_key(matched) != service.cache_key(mismatched)
    # 旧 build 的 KG 数值不得出现在当前 build 的语义缓存载荷中
    payload = evidence_module._semantic_evidence_payload(mismatched)
    assert all(key not in (payload["phase_f"] or {}) for key in PHASE_F_KG_KEYS)
    assert "131" not in json.dumps(payload["phase_f"], ensure_ascii=False)


def test_semantic_hash_ignores_stale_kg_values_in_mismatched_row():
    # 携带旧 KG 数值的行与已剥离行，在隔离状态下必须产生同一缓存身份
    polluted = assemble_with_phase_f(make_phase_f_row(), make_phase_f_provenance("mismatch"))
    safe = assemble_with_phase_f(make_phase_f_row(with_kg=False), make_phase_f_provenance("mismatch"))
    assert evidence_module.semantic_evidence_sha256(polluted) == evidence_module.semantic_evidence_sha256(safe)


def _install_collect_evidence_mocks(monkeypatch, tmp_path, phase_f_result):
    """Isolate collect_evidence from Neo4j/PostgreSQL while keeping the real
    Phase F gating path under test."""
    calls = {}
    project = SimpleNamespace(project_id="proj", slug="erpos-gse42568")
    build = SimpleNamespace(graph_build_id=CURRENT_BUILD)
    s1_file = tmp_path / "limma.csv"
    s1_file.write_text("gene_symbol,logFC\nHDAC1,1.0\n", encoding="utf-8")

    monkeypatch.setattr(evidence_module, "load_frozen_top10", lambda: ([{"pert_id": "BRD-K16485616", "rank": "2", "drug_name": "mocetinostat", "WTCS_median": "-0.5"}], "b" * 64))
    monkeypatch.setattr(evidence_module, "resolve_graph_build", lambda db, project_id, requested: build)
    monkeypatch.setattr(evidence_module, "_project_bundle_dir", lambda db, proj, b: Path("."))
    monkeypatch.setattr(evidence_module, "S1_RESULTS_PATH", s1_file)
    monkeypatch.setattr(evidence_module, "_read_rows", lambda: ())
    monkeypatch.setattr(evidence_module, "load_grade_matrix_row", lambda drug_id: None)

    def fake_package(db, proj, b, bundle_dir, drug_id, expected_graph_build_id=None):
        calls["package_expected"] = expected_graph_build_id
        package = make_package()
        package["graph_build_id"] = b.graph_build_id
        return package

    def fake_phase_f(drug_id, proj, expected_graph_build_id=None):
        calls["phase_f_expected"] = expected_graph_build_id
        return phase_f_result

    monkeypatch.setattr(evidence_module.kg06, "build_evidence_package", fake_package)
    monkeypatch.setattr(evidence_module.kg06, "_read_manifested_phase_f_row", fake_phase_f)
    monkeypatch.setattr(evidence_module.kg06, "_load_exp006_row", lambda drug_id, proj: (None, {}))
    return calls, SimpleNamespace(get=lambda model, key: project)


def test_collect_evidence_passes_current_build_and_gates_mismatch(monkeypatch, tmp_path):
    calls, db = _install_collect_evidence_mocks(
        monkeypatch, tmp_path, (make_phase_f_row(with_kg=False), make_phase_f_provenance("mismatch"))
    )
    bundle = evidence_module.collect_evidence(db, "proj", "BRD-K16485616")
    assert calls["package_expected"] == CURRENT_BUILD
    assert calls["phase_f_expected"] == CURRENT_BUILD
    assert bundle["graph_build_id"] == CURRENT_BUILD
    assert all(key not in bundle["phase_f"] for key in PHASE_F_KG_KEYS)
    assert bundle["phase_f"]["external_s1_median_rank"] == "26"
    assert bundle["phase_f_provenance"]["reason"] == "graph_build_mismatch"


def test_collect_evidence_matching_build_keeps_kg_stats(monkeypatch, tmp_path):
    calls, db = _install_collect_evidence_mocks(
        monkeypatch, tmp_path, (make_phase_f_row(), make_phase_f_provenance("match"))
    )
    bundle = evidence_module.collect_evidence(db, "proj", "BRD-K16485616")
    assert calls["phase_f_expected"] == CURRENT_BUILD
    assert bundle["phase_f"]["kg_supported_route_count"] == "131"


def test_collect_evidence_hash_mismatch_keeps_phase_f_unavailable(monkeypatch, tmp_path):
    calls, db = _install_collect_evidence_mocks(
        monkeypatch, tmp_path, (None, make_phase_f_provenance("hash_mismatch"))
    )
    bundle = evidence_module.collect_evidence(db, "proj", "BRD-K16485616")
    assert bundle["phase_f"] is None
    assert bundle["phase_f_provenance"]["reason"] == "hash_mismatch"

