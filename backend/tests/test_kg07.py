import copy
import json
from pathlib import Path

import pytest

from app import kg07
from app.kg07_adapter import build_kg07_evidence

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PATH = (
    ROOT
    / "experiments"
    / "kg06_mechanism_evidence"
    / "results"
    / "evidence_packages"
    / "BRD-K44227013.json"
)


@pytest.fixture
def package() -> dict:
    assert PACKAGE_PATH.is_file(), f"missing real package: {PACKAGE_PATH}"
    return json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def adapter(package: dict) -> dict:
    return build_kg07_evidence(package)


def test_adapter_annotation_count_uses_annotations_not_unique_targets(adapter: dict) -> None:
    target = adapter["dimensions"]["target_coverage"]
    annotations = target["annotations"]
    targets = target["targets"]
    assert len(annotations) == 21
    assert len(targets) == 21
    assert adapter["validator_bundle"]["counts"]["direct_target_annotations"] == len(annotations)
    assert adapter["llm_validator_bundle"]["counts"]["direct_target_annotations"] == len(annotations)
    assert adapter["validator_bundle"]["counts"]["unique_target_genes"] == len(targets)


def test_adapter_llm_signature_coverage_comes_from_dimensions(adapter: dict) -> None:
    signature = adapter["dimensions"]["signature_coverage"]["metrics"]
    llm_signature = adapter["llm_input"]["signature_coverage"]
    assert llm_signature["n_signatures"] == signature["n_signatures"] == 1
    assert llm_signature["replicates_median"] == signature["replicates_median"] == 3.0
    assert llm_signature["primary_dose"] == signature["primary_dose"] == "10.0 um"
    assert llm_signature["n_dose_conditions"] == signature["n_dose_conditions"] == 5
    assert llm_signature["signature_level"] is None


def test_rule_template_keeps_legacy_sections_and_metadata(package: dict) -> None:
    result = kg07.rule_template(package)
    assert result["status"] == "rule_template"
    assert result["mode"] == "kg_rule"
    assert result["graph_build_id"] == package["graph_build_id"]
    assert result["model"] is None
    assert result["cached"] is False
    assert result["provider_cost_status"] == "not_requested_deterministic_rule"
    for key in kg07.SECTION_KEYS:
        assert key in result["explanation"]
    for key in (
        "status",
        "mode",
        "candidate",
        "project_id",
        "graph_build_id",
        "evidence_input_sha256",
        "prompt_version",
        "model",
        "cached",
        "latency_seconds",
        "usage",
        "provider_cost",
        "provider_cost_status",
        "semantic_entailment_status",
        "human_review_required",
        "citation_validation_issues",
        "citation_validation_pass",
        "citation_reference_validation_pass",
        "evidence_gaps",
        "coverage",
        "explanation",
        "dimensions",
        "full_counts",
        "scoped_counts",
    ):
        assert key in result


def test_rule_target_counts_actions_and_real_symbols(package: dict) -> None:
    result = kg07.rule_template(package)
    target_claims = result["explanation"]["known_targets"]
    summary = target_claims[0]["text"]
    assert "21 条药物—基因注释" in summary
    assert "21 个唯一基因" in summary
    assert "TARGET 15" in summary
    assert "ENZYME 4" in summary
    assert "TRANSPORTER 2" in summary
    assert "注释数量不等同于独立靶点数量" in summary
    symbols = {claim["genes"][0]["symbol"] for claim in target_claims[1:] if claim["genes"]}
    assert {"ABL1", "BCR"}.issubset(symbols)
    assert len(target_claims) == 22


def test_rule_pathway_counts_and_representatives(package: dict) -> None:
    result = kg07.rule_template(package)
    pathway_claims = result["explanation"]["associated_pathways"]
    summary = pathway_claims[0]["text"]
    assert "131 条" in summary
    assert "99 条唯一通路" in summary
    assert "12 条代表通路" in summary
    assert len(pathway_claims) == 13
    assert all("不代表激活或抑制机制" in claim["text"] for claim in pathway_claims[1:])
    assert all("不代表因果" in claim["text"] for claim in pathway_claims[1:])


def test_rule_s2_without_evidence_ids_is_empty_and_marked_gap(package: dict) -> None:
    result = kg07.rule_template(package)
    assert result["explanation"]["screening_basis"] == []
    gaps = " ".join(item["text"] for item in result["explanation"]["insufficient_evidence"])
    assert "S2" in gaps
    assert "EvidenceRecord" in gaps
    assert any(
        "s2_condition_evidence_ids" in field for field in result["missing_fields"]
    )


def test_rule_independent_validation_is_display_only(package: dict) -> None:
    result = kg07.rule_template(package)
    claims = result["explanation"]["independent_validation"]
    assert len(claims) == 1
    claim = claims[0]
    assert claim["display_only"] is True
    assert claim["evidence_ids"] == []
    assert claim["path_ids"] == []
    assert "确定性展示" in claim["text"]
    limitations = " ".join(result["explanation"]["limitations"])
    assert "相同 LINCS" in limitations
    assert "PRISM" in limitations


def test_rule_never_creates_mechanism_hypotheses_or_level5(package: dict) -> None:
    result = kg07.rule_template(package)
    assert result["explanation"]["mechanism_hypotheses"] == []
    assert result["dimensions"]["signature_coverage"]["metrics"]["signature_level"] is None
    assert "Level5" in " ".join(result["explanation"]["limitations"])
    assert "不是独立生物学重复" in " ".join(result["explanation"]["limitations"])


def test_unimplemented_ai_modes_do_not_call_old_provider(
    package: dict, monkeypatch
) -> None:
    monkeypatch.setattr(kg07.settings, "deepseek_api_key", "")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    for mode in ("kg_llm", "direct_llm"):
        result = kg07.interpret(package, mode)
        assert result["status"] == "not_implemented"
        assert result["mode"] == mode
        assert result["model"] is None
        assert result["dimensions"] is None
        assert "no provider call" in (result["message"] or "")
    assert not hasattr(kg07, "_post_chat")
    assert not hasattr(kg07, "_validate_claims")


def test_empty_paths_are_safe_deterministic_fallback(package: dict) -> None:
    empty = copy.deepcopy(package)
    empty["paths"] = []
    result = kg07.rule_template(empty)
    assert result["status"] == "rule_template"
    assert result["degraded"] is True
    assert result["dimensions"] is None
    assert result["dimensions_status"] == "not_available_no_paths"
    assert result["explanation"]["known_targets"] == []
    assert result["explanation"]["associated_pathways"] == []
    assert result["explanation"]["mechanism_hypotheses"] == []
    assert result["explanation"]["insufficient_evidence"]


def test_expected_graph_build_mismatch_is_rejected(package: dict) -> None:
    with pytest.raises(ValueError, match="does not match"):
        kg07.rule_template(
            package,
            expected_graph_build_id="GRAPH_BUILD:different-production-build",
        )


def test_invalid_mode_is_rejected(package: dict) -> None:
    with pytest.raises(ValueError, match="unsupported KG07 mode"):
        kg07.interpret(package, "not_a_mode")
