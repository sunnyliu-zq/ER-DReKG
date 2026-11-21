"""Stage 2A-2 tests for KG07 DeepSeek Flash + Top10 Validator integration.

All provider calls are mocked. The real Ponatinib package is used only as a
local, read-only fixture; no database, Neo4j, or network access is involved.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from app import kg07
from app.interpretation import provider, validator
from app.kg07_adapter import build_kg07_evidence

_ORIGINAL_SETTINGS_API_KEY = kg07.settings.deepseek_api_key
_ORIGINAL_ENV_API_KEY = os.environ.get("DEEPSEEK_API_KEY")


@pytest.fixture(autouse=True)
def isolate_provider_config(monkeypatch):
    """Keep every test off the developer .env key and restore it afterwards."""

    monkeypatch.setattr(kg07.settings, "deepseek_api_key", "")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    yield
    monkeypatch.undo()
    assert kg07.settings.deepseek_api_key == _ORIGINAL_SETTINGS_API_KEY
    assert os.environ.get("DEEPSEEK_API_KEY") == _ORIGINAL_ENV_API_KEY


def test_provider_config_is_isolated_at_test_start() -> None:
    assert kg07.settings.deepseek_api_key == ""
    assert os.environ.get("DEEPSEEK_API_KEY") is None

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PATH = (
    ROOT
    / "experiments"
    / "kg06_mechanism_evidence"
    / "results"
    / "evidence_packages"
    / "BRD-K44227013.json"
)
V2_PACKAGE_PATH = (
    ROOT
    / "experiments"
    / "kg06_mechanism_evidence"
    / "results"
    / "evidence_packages_v2"
    / "BRD-K44227013.json"
)

ABL1_GENE_ID = "ENSG00000097007"
ABL1_PATH = "04e454c03d43153f617422cda58483118d98b2da0fdf025116b6dcd24f77d929"
ABL1_SAFE_EVIDENCE = "ae2cddaa0d404a7ed81af5e26f68425e659022a86d807e344b311e288249361e"
ABL1_SHARED_EVIDENCE = (
    "08901cf4fd535debe90f3be855e16d49916b5e045ab6737a8df1d3b117504571",
    "27b215ca59ac04dae9d2cf42fedde86d546f8097349f7258f087f79bcfd3bf46",
)
PATHWAY_919 = "919007a796dadb35e55b70590740e5db2dace9c874269f95dcb1d94349fe88ee"
PATHWAY_SAFE_EVIDENCE = (
    "0c41c7c24591437d8ca2b5453a9be7827c2ba02f580675faa9b9ee242382018e",
    "9cb5e7fd05803f2852beba9fc8903b2ecf381be1c84e38a959c81eea710504b0",
)


@pytest.fixture
def package() -> dict:
    assert PACKAGE_PATH.is_file(), f"missing real package: {PACKAGE_PATH}"
    return json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def v2_package() -> dict:
    assert V2_PACKAGE_PATH.is_file(), f"missing real v2 package: {V2_PACKAGE_PATH}"
    return json.loads(V2_PACKAGE_PATH.read_text(encoding="utf-8"))


def _real_s2_evidence_id(package: dict) -> str:
    conditions = package["screening_evidence"]["s2_conditions"]
    for condition in conditions:
        for evidence_id in condition.get("evidence_ids") or []:
            return evidence_id
    raise AssertionError("real package has no S2 evidence ID")


def _screening_claim(evidence_id: str, text: str = "10.0 um 条件下 WTCS 为负，仅表示表达逆转关联。") -> dict:
    return {
        "screening_rationale": [
            {
                "text": text,
                "evidence_ids": [evidence_id],
                "path_ids": [],
                "relation": "association",
                "layer": "A",
            }
        ]
    }


def _cross_condition_duplicate_package(package: dict) -> dict:
    duplicate = copy.deepcopy(package)
    conditions = duplicate["screening_evidence"]["s2_conditions"]
    shared_id = conditions[0]["evidence_ids"][0]
    source = copy.deepcopy(conditions[1]["sources"][0])
    source["evidence_id"] = shared_id
    source["object_id"] = conditions[1]["condition"]["id"]
    conditions[1]["evidence_ids"] = [shared_id]
    conditions[1]["sources"] = [source]
    return duplicate


def _duplicate_source_package(package: dict, *, identical: bool) -> dict:
    duplicate = copy.deepcopy(package)
    condition = duplicate["screening_evidence"]["s2_conditions"][0]
    source = copy.deepcopy(condition["sources"][0])
    if not identical:
        source["source_record_id"] = f"{source['source_record_id']}:duplicate"
        source["assertion_sha256"] = "0" * 64
    condition["sources"].append(source)
    return duplicate


@pytest.fixture
def adapter(package: dict) -> dict:
    return build_kg07_evidence(package)


def valid_ai_raw() -> dict:
    return {
        "screening_rationale": [],
        "target_mechanism": [
            {
                "text": "ABL1 存在直接靶点注释，仅表示药物—基因关联。",
                "evidence_ids": [ABL1_SAFE_EVIDENCE],
                "path_ids": [ABL1_PATH],
                "relation": "association",
                "layer": "A",
                "genes": [{"gene_id": ABL1_GENE_ID, "symbol": "ABL1"}],
            }
        ],
        "pathway_association": [
            {
                "text": "HALLMARK_ESTROGEN_RESPONSE_EARLY 与靶点证据存在通路交汇。",
                "evidence_ids": [PATHWAY_SAFE_EVIDENCE[0]],
                "path_ids": [PATHWAY_919],
                "relation": "association",
                "layer": "B",
            }
        ],
        "mechanism_hypotheses": [
            {
                "text": "待验证假设：ABL1 与雌激素反应早期通路可能存在关联。",
                "evidence_ids": [ABL1_SAFE_EVIDENCE, PATHWAY_SAFE_EVIDENCE[0]],
                "path_ids": [ABL1_PATH, PATHWAY_919],
                "relation": "association",
                "layer": "C",
            }
        ],
    }


def install_mock(
    monkeypatch,
    raw: dict,
    *,
    model: str = "deepseek-flash",
    max_tokens: int | None = None,
) -> list[dict]:
    monkeypatch.setattr(kg07.settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(kg07.settings, "deepseek_model", "deepseek-flash")
    calls: list[dict] = []

    def fake(messages, *, max_tokens: int | None = None):
        calls.append({"messages": messages, "max_tokens": max_tokens})
        return raw, {
            "model": model,
            "latency_seconds": 0.25,
            "usage": {"prompt_tokens": 10, "completion_tokens": 8},
            "finish_reason": "stop",
        }

    monkeypatch.setattr(kg07.provider, "chat_json", fake)
    return calls


def test_real_kg_llm_passes_top10_validator(package: dict, monkeypatch) -> None:
    calls = install_mock(monkeypatch, valid_ai_raw())
    result = kg07.interpret(package, "kg_llm")

    assert calls and calls[0]["max_tokens"] == 8192
    assert result["status"] == "generated"
    assert result["mode"] == "kg_llm"
    assert result["model"] == "deepseek-flash"
    assert result["prompt_version"] == kg07.AI_PROMPT_VERSION
    assert result["citation_validation_pass"] is True
    assert result["citation_reference_validation_pass"] is True
    assert result["layer_validation_pass"] is True
    assert result["claim_audit"]["accepted_claims"] == 3
    assert result["claim_audit"]["dropped_claims"] == 0
    assert result["explanation"]["screening_basis"] == []

    known = result["explanation"]["known_targets"]
    assert len(known) == 2
    assert "21 条药物—基因注释" in known[0]["text"]
    assert known[1]["genes"][0]["symbol"] == "ABL1"
    assert known[1]["layer"] == "A"
    assert known[1]["citation_reference_status"] == "top10_validator_checked"

    pathways = result["explanation"]["associated_pathways"]
    assert len(pathways) == 2
    assert "131 条" in pathways[0]["text"]
    assert "99 条唯一通路" in pathways[0]["text"]
    assert pathways[1]["layer"] == "B"

    hypotheses = result["explanation"]["mechanism_hypotheses"]
    assert len(hypotheses) == 1
    assert hypotheses[0]["layer"] == "C"
    assert hypotheses[0]["text"].startswith("待验证假设")

    assert result["explanation"]["independent_validation"][0]["display_only"] is True
    assert result["coverage_assessment"]["coverage_level"] == "coverage_high"
    assert result["coverage_bundle_scope"] == "full"
    assert result["full_counts"]["unique_evidence"] == 258
    assert result["scoped_counts"]["unique_evidence"] == 52
    assert result["cached"] is False


def test_ai_payload_is_bounded_and_has_safe_citation_hints(
    adapter: dict, monkeypatch
) -> None:
    monkeypatch.setattr(kg07.settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(kg07.settings, "deepseek_model", "deepseek-flash")
    payload = json.loads(
        kg07._build_ai_messages(
            kg07._bounded_ai_payload(adapter, mode="kg_llm"), mode="kg_llm"
        )[1]["content"]
    )

    assert "pathway_counts" not in payload
    assert "target_annotation_count" not in payload
    assert "target_unique_gene_count" not in payload
    assert len(payload["targets"]) == 21
    assert len(payload["pathways"]) == 12
    assert payload["screening_available"] is False
    assert payload["s2_conditions"] == []

    abl1 = next(row for row in payload["targets"] if row["symbol"] == "ABL1")
    assert abl1["safe_target_path_ids"] == [ABL1_PATH]
    assert abl1["safe_target_evidence_ids"] == [ABL1_SAFE_EVIDENCE]

    pathway = next(
        row for row in payload["pathways"] if row["safe_pathway_path_id"] == PATHWAY_919
    )
    assert pathway["safe_pathway_evidence_ids"] == list(PATHWAY_SAFE_EVIDENCE)


def test_safe_citation_hints_come_from_real_path_membership(adapter: dict) -> None:
    scoped = adapter["llm_validator_bundle"]
    evidence_meta = scoped["evidence_meta"]
    path_meta = scoped["path_meta"]

    abl1 = next(
        row for row in kg07._target_citation_hints(scoped) if row["symbol"] == "ABL1"
    )
    assert all(
        evidence_id in path_meta[ABL1_PATH]["evidence_ids"]
        and ABL1_PATH in evidence_meta[evidence_id]["path_ids"]
        for evidence_id in abl1["safe_target_evidence_ids"]
    )
    assert abl1["safe_target_evidence_ids"] == [ABL1_SAFE_EVIDENCE]

    pathway = next(
        row
        for row in kg07._pathway_citation_hints(scoped)
        if row["safe_pathway_path_id"] == PATHWAY_919
    )
    assert all(
        evidence_id in path_meta[PATHWAY_919]["evidence_ids"]
        and PATHWAY_919 in evidence_meta[evidence_id]["path_ids"]
        for evidence_id in pathway["safe_pathway_evidence_ids"]
    )
    assert pathway["safe_pathway_evidence_ids"] == list(PATHWAY_SAFE_EVIDENCE)


def test_mixed_target_evidence_is_rejected_by_validator(package: dict, monkeypatch) -> None:
    raw = valid_ai_raw()
    raw["target_mechanism"] = [
        {
            "text": "ABL1 存在直接靶点注释。",
            "evidence_ids": [ABL1_SHARED_EVIDENCE[0], ABL1_SHARED_EVIDENCE[1], ABL1_SAFE_EVIDENCE],
            "path_ids": [ABL1_PATH],
            "relation": "association",
            "layer": "A",
            "genes": [{"gene_id": ABL1_GENE_ID, "symbol": "ABL1"}],
        }
    ]
    install_mock(monkeypatch, raw)
    result = kg07.interpret(package, "kg_llm")

    assert result["status"] == "generated"
    assert result["claim_audit"]["accepted_claims"] == 2
    assert result["claim_audit"]["dropped_claims"] == 1
    assert len(result["explanation"]["known_targets"]) == 1
    assert result["explanation"]["known_targets"][0]["display_only"] is True
    assert result["citation_validation_pass"] is False
    assert result["citation_reference_validation_pass"] is False
    assert any(
        "invalid_routes" in issue or "citation type" in issue.get("issue", "")
        for issue in result["validator_issues"]
    )


def test_pathway_target_evidence_is_rejected_by_validator(package: dict, monkeypatch) -> None:
    raw = valid_ai_raw()
    raw["pathway_association"] = [
        {
            "text": "HALLMARK_ESTROGEN_RESPONSE_EARLY 与靶点证据存在通路交汇。",
            "evidence_ids": [ABL1_SHARED_EVIDENCE[0]],
            "path_ids": [PATHWAY_919],
            "relation": "association",
            "layer": "B",
        }
    ]
    install_mock(monkeypatch, raw)
    result = kg07.interpret(package, "kg_llm")

    assert result["citation_validation_pass"] is False
    assert result["citation_reference_validation_pass"] is False
    assert any("citation type" in issue.get("issue", "") for issue in result["validator_issues"])


def test_s2_without_evidence_ids_cannot_produce_screening_claim(
    package: dict, monkeypatch
) -> None:
    raw = valid_ai_raw()
    raw["screening_rationale"] = [
        {
            "text": "S2 条件可支持筛选结论。",
            "evidence_ids": ["synthetic-not-a-real-s2-id"],
            "path_ids": [],
            "relation": "association",
            "layer": "A",
        }
    ]
    install_mock(monkeypatch, raw)
    result = kg07.interpret(package, "kg_llm")

    assert result["screening_available"] is False
    assert result["ai_screening_suppressed"] is True
    assert result["explanation"]["screening_basis"] == []
    assert result["status"] == "generated"
    assert any(
        "screening claim suppressed" in issue.get("issue", "")
        for issue in result["pre_validator_issues"]
    )
    assert result["citation_validation_issues"] == []
    assert result["validator_issues"] == []
    assert any(
        "S2" in item["text"]
        for item in result["explanation"]["insufficient_evidence"]
    )


def test_direct_llm_without_s2_ids_does_not_call_provider(
    package: dict, monkeypatch
) -> None:
    monkeypatch.setattr(kg07.settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(kg07.settings, "deepseek_model", "deepseek-flash")
    called = {"count": 0}

    def fail(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(kg07.provider, "chat_json", fail)
    result = kg07.interpret(package, "direct_llm")

    assert called["count"] == 0
    assert result["status"] == "coverage_insufficient"
    assert result["model"] is None
    assert result["screening_available"] is False
    assert result["explanation"]["known_targets"] == []
    assert result["explanation"]["associated_pathways"] == []
    assert result["explanation"]["mechanism_hypotheses"] == []
    assert result["coverage_assessment"]["coverage_level"] == "coverage_high"


def test_direct_llm_with_real_s2_id_uses_authenticated_restricted_bundle(
    v2_package: dict, monkeypatch
) -> None:
    s2_id = _real_s2_evidence_id(v2_package)
    assert s2_id not in {
        evidence_id
        for path in v2_package["paths"]
        for evidence_id in path.get("evidence_ids") or []
    }
    calls = install_mock(monkeypatch, _screening_claim(s2_id))

    result = kg07.interpret(v2_package, "direct_llm")

    assert calls and len(calls) == 1
    payload = json.loads(calls[0]["messages"][1]["content"])
    assert payload["screening_available"] is True
    assert all(row["evidence_ids"] for row in payload["s2_conditions"])
    assert len(payload["s2_conditions"]) == 5
    assert payload["targets"] == []
    assert payload["pathways"] == []

    assert result["status"] == "generated"
    assert result["screening_available"] is True
    assert result["validator_bundle_scope"] == "llm_scoped"
    assert result["model"] == "deepseek-flash"
    assert result["explanation"]["known_targets"] == []
    assert result["explanation"]["associated_pathways"] == []
    assert result["explanation"]["mechanism_hypotheses"] == []
    assert result["claim_audit"]["accepted_claims"] == 1
    assert result["claim_audit"]["dropped_claims"] == 0
    assert result["citation_validation_pass"] is True
    assert result["citation_reference_validation_pass"] is True
    assert result["layer_validation_pass"] is True
    screening = result["explanation"]["screening_basis"]
    assert len(screening) == 1
    assert screening[0]["evidence_ids"] == [s2_id]
    assert screening[0]["path_ids"] == []


def _forbidden_provider(monkeypatch):
    monkeypatch.setattr(kg07.settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(kg07.settings, "deepseek_model", "deepseek-flash")
    calls: list[dict] = []

    def fail(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(kg07.provider, "chat_json", fail)
    return calls


def _assert_provider_was_not_called(package: dict, monkeypatch) -> None:
    calls = _forbidden_provider(monkeypatch)
    result = kg07.interpret(package, "direct_llm")

    assert calls == []
    assert result["status"] == "coverage_insufficient"
    assert result["screening_available"] is False
    assert result["model"] is None
    assert result["explanation"]["screening_basis"] == []
    assert result["explanation"]["known_targets"] == []
    assert result["explanation"]["associated_pathways"] == []
    assert result["explanation"]["mechanism_hypotheses"] == []


def test_direct_llm_rejects_fake_s2_id_before_provider(
    v2_package: dict, monkeypatch
) -> None:
    fake = copy.deepcopy(v2_package)
    fake["screening_evidence"]["s2_conditions"][0]["evidence_ids"].append(
        "fake-not-in-real-package"
    )
    _assert_provider_was_not_called(fake, monkeypatch)


def test_direct_llm_rejects_target_evidence_in_s2_condition(
    v2_package: dict, monkeypatch
) -> None:
    target = copy.deepcopy(v2_package)
    target["screening_evidence"]["s2_conditions"][0]["evidence_ids"].append(
        ABL1_SAFE_EVIDENCE
    )
    _assert_provider_was_not_called(target, monkeypatch)


def test_direct_llm_rejects_pathway_evidence_in_s2_condition(
    v2_package: dict, monkeypatch
) -> None:
    pathway = copy.deepcopy(v2_package)
    pathway["screening_evidence"]["s2_conditions"][0]["evidence_ids"].append(
        PATHWAY_SAFE_EVIDENCE[0]
    )
    _assert_provider_was_not_called(pathway, monkeypatch)


def test_direct_llm_empty_s2_condition_list_does_not_call_provider(
    package: dict, monkeypatch
) -> None:
    empty = copy.deepcopy(package)
    empty["screening_evidence"]["s2_conditions"] = []
    _assert_provider_was_not_called(empty, monkeypatch)


def test_direct_llm_keeps_only_certified_conditions_when_some_are_empty(
    v2_package: dict, monkeypatch
) -> None:
    mixed = copy.deepcopy(v2_package)
    conditions = mixed["screening_evidence"]["s2_conditions"]
    conditions[0]["evidence_ids"] = []
    conditions[0]["sources"] = []
    valid_id = conditions[-1]["evidence_ids"][0]
    calls = install_mock(monkeypatch, _screening_claim(valid_id))

    result = kg07.interpret(mixed, "direct_llm")

    assert calls and len(calls) == 1
    payload = json.loads(calls[0]["messages"][1]["content"])
    assert len(payload["s2_conditions"]) == 4
    assert all(row["evidence_ids"] for row in payload["s2_conditions"])
    assert result["status"] == "generated"
    assert result["screening_available"] is True
    assert result["explanation"]["screening_basis"]


def test_direct_llm_partial_valid_and_invalid_s2_ids_degrades_before_provider(
    v2_package: dict, monkeypatch
) -> None:
    mixed = copy.deepcopy(v2_package)
    mixed["screening_evidence"]["s2_conditions"][0]["evidence_ids"].append(
        "fake-but-non-empty"
    )
    _assert_provider_was_not_called(mixed, monkeypatch)


def _assert_kg_llm_provider_payload_has_no_s2(package: dict, monkeypatch) -> None:
    calls = install_mock(monkeypatch, valid_ai_raw())
    result = kg07.interpret(package, "kg_llm")

    assert calls and len(calls) == 1
    payload = json.loads(calls[0]["messages"][1]["content"])
    assert payload["screening_available"] is False
    assert payload["s2_conditions"] == []
    assert result["screening_available"] is False


def test_kg_llm_cross_condition_duplicate_id_has_no_s2_payload(
    v2_package: dict, monkeypatch
) -> None:
    _assert_kg_llm_provider_payload_has_no_s2(
        _cross_condition_duplicate_package(v2_package), monkeypatch
    )


@pytest.mark.parametrize("identical", [False, True])
def test_kg_llm_duplicate_source_has_no_s2_payload(
    v2_package: dict, monkeypatch, identical: bool
) -> None:
    _assert_kg_llm_provider_payload_has_no_s2(
        _duplicate_source_package(v2_package, identical=identical), monkeypatch
    )


def test_direct_llm_cross_condition_duplicate_id_does_not_call_provider(
    v2_package: dict, monkeypatch
) -> None:
    _assert_provider_was_not_called(
        _cross_condition_duplicate_package(v2_package), monkeypatch
    )


@pytest.mark.parametrize("identical", [False, True])
def test_direct_llm_duplicate_source_does_not_call_provider(
    v2_package: dict, monkeypatch, identical: bool
) -> None:
    _assert_provider_was_not_called(
        _duplicate_source_package(v2_package, identical=identical), monkeypatch
    )


def test_direct_llm_dropping_all_claims_does_not_show_raw_claims(
    v2_package: dict, monkeypatch
) -> None:
    s2_id = _real_s2_evidence_id(v2_package)
    raw_text = "这条模型原始文本不能被展示"
    calls = install_mock(monkeypatch, _screening_claim(ABL1_SAFE_EVIDENCE, raw_text))

    result = kg07.interpret(v2_package, "direct_llm")

    assert calls and len(calls) == 1
    assert result["status"] == "generated_no_valid_claims"
    assert result["screening_available"] is True
    assert result["claim_audit"]["accepted_claims"] == 0
    assert result["claim_audit"]["dropped_claims"] == 1
    assert result["explanation"]["screening_basis"] == []
    assert raw_text not in json.dumps(result, ensure_ascii=False)
    assert s2_id not in json.dumps(result["explanation"], ensure_ascii=False)


def test_empty_paths_ai_mode_is_safe_and_does_not_call_provider(
    package: dict, monkeypatch
) -> None:
    monkeypatch.setattr(kg07.settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(kg07.settings, "deepseek_model", "deepseek-flash")

    def fail(*args, **kwargs):
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(kg07.provider, "chat_json", fail)
    empty = copy.deepcopy(package)
    empty["paths"] = []
    result = kg07.interpret(empty, "kg_llm")

    assert result["status"] == "coverage_insufficient"
    assert result["degraded"] is True
    assert result["dimensions"] is None
    assert result["dimensions_status"] == "not_available_no_paths"
    assert result["explanation"]["mechanism_hypotheses"] == []
    assert result["explanation"]["insufficient_evidence"]


def test_structure_is_rejected_before_validator(package: dict, monkeypatch) -> None:
    raw = valid_ai_raw()
    raw["limitations"] = ["not allowed"]
    install_mock(monkeypatch, raw)

    def fail(*args, **kwargs):
        raise AssertionError("validator must not run for invalid structure")

    monkeypatch.setattr(kg07.validator, "validate_explanation", fail)
    with pytest.raises(kg07.KG07AIStructureError):
        kg07.interpret(package, "kg_llm")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["target_mechanism"][0].update({"text": "很" * 61}),
        lambda raw: raw["target_mechanism"].extend(
            [copy.deepcopy(raw["target_mechanism"][0]) for _ in range(3)]
        ),
        lambda raw: raw["target_mechanism"][0].pop("genes"),
        lambda raw: raw["target_mechanism"][0].update(
            {"evidence_ids": [], "path_ids": []}
        ),
    ],
)
def test_structure_limits_and_required_fields(package: dict, monkeypatch, mutate) -> None:
    raw = valid_ai_raw()
    mutate(raw)
    install_mock(monkeypatch, raw)
    with pytest.raises(kg07.KG07AIStructureError):
        kg07.interpret(package, "kg_llm")


def test_preflight_model_lock_rejects_non_flash_without_provider_call(
    package: dict, monkeypatch
) -> None:
    monkeypatch.setattr(kg07.settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(kg07.settings, "deepseek_model", "deepseek-chat")

    def fail(*args, **kwargs):
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(kg07.provider, "chat_json", fail)
    with pytest.raises(provider.ProviderConfigError):
        kg07.interpret(package, "kg_llm")


def test_postflight_model_lock_rejects_wrong_metadata_model(
    package: dict, monkeypatch
) -> None:
    install_mock(monkeypatch, valid_ai_raw(), model="deepseek-chat")
    with pytest.raises(kg07.KG07AIResponseError, match="metadata.model"):
        kg07.interpret(package, "kg_llm")


def test_provider_error_types_are_mapped_distinctly(package: dict, monkeypatch) -> None:
    monkeypatch.setattr(kg07.settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(kg07.settings, "deepseek_model", "deepseek-flash")

    def raise_auth(*args, **kwargs):
        raise provider.ProviderAuthError("auth")

    monkeypatch.setattr(kg07.provider, "chat_json", raise_auth)
    with pytest.raises(provider.ProviderAuthError):
        kg07.interpret(package, "kg_llm")

    def raise_request(*args, **kwargs):
        raise provider.ProviderRequestError("request")

    monkeypatch.setattr(kg07.provider, "chat_json", raise_request)
    with pytest.raises(kg07.KG07AIResponseError) as request_error:
        kg07.interpret(package, "kg_llm")
    assert issubclass(request_error.type, ValueError)

    def raise_response(*args, **kwargs):
        raise provider.ProviderResponseError("response")

    monkeypatch.setattr(kg07.provider, "chat_json", raise_response)
    with pytest.raises(kg07.KG07AIResponseError) as response_error:
        kg07.interpret(package, "kg_llm")
    assert issubclass(response_error.type, ValueError)


def test_coverage_is_computed_from_full_bundle_only(
    package: dict, monkeypatch
) -> None:
    calls: list[str | None] = []
    original = validator.backend_coverage

    def spy(bundle):
        calls.append(bundle.get("scope"))
        return original(bundle)

    monkeypatch.setattr(kg07.validator, "backend_coverage", spy)
    install_mock(monkeypatch, valid_ai_raw())
    result = kg07.interpret(package, "kg_llm")

    assert calls == ["full"]
    assert result["coverage_bundle_scope"] == "full"
    assert result["coverage_assessment"]["unique_evidence_count"] == 258


def test_no_verifiable_citation_is_reference_failure(
    package: dict, adapter: dict
) -> None:
    scoped = adapter["llm_validator_bundle"]
    clean, issues, audit = validator.validate_explanation(
        {
            "target_mechanism": [
                {
                    "text": "ABL1 存在直接靶点注释。",
                    "evidence_ids": [],
                    "path_ids": [],
                    "relation": "association",
                    "layer": "A",
                    "genes": [{"gene_id": ABL1_GENE_ID, "symbol": "ABL1"}],
                }
            ]
        },
        dict(scoped),
    )
    assert audit["dropped_claims"] == 1
    assert any(
        issue["issue"] == "claim dropped: no verifiable citation"
        for issue in audit["issues"]
    )

    result = kg07._ai_result(
        package,
        adapter,
        mode="kg_llm",
        clean=clean,
        issues=issues,
        audit=audit,
        metadata={"model": "deepseek-flash", "latency_seconds": 0.0},
        coverage=kg07._full_coverage(adapter),
        screening_available=False,
        screening_suppressed=False,
        pre_validator_issues=[],
    )

    assert result["claim_audit"]["accepted_claims"] == 0
    assert result["citation_validation_pass"] is False
    assert result["citation_reference_validation_pass"] is False
    assert result["validator_issue_categories"] == {"reference": 1}
    assert result["citation_validation_issues"] == audit["issues"]


@pytest.mark.parametrize(
    "issue_text",
    [
        "claim dropped: no verifiable citation",
        "claim dropped: one or more citation IDs are not in the current evidence bundle",
        "cited evidence_id is not part of the cited path_id",
        "unknown evidence_ids removed",
        "unknown path_ids removed",
    ],
)
def test_reference_issue_classification_covers_validator_messages(
    issue_text: str,
) -> None:
    assert kg07._classify_validator_issue({"issue": issue_text}) == "reference"


def test_layer_mismatch_fails_layer_but_not_reference(
    package: dict, monkeypatch
) -> None:
    raw = valid_ai_raw()
    raw["target_mechanism"][0]["layer"] = "B"
    install_mock(monkeypatch, raw)
    result = kg07.interpret(package, "kg_llm")

    assert result["claim_audit"]["accepted_claims"] == 3
    assert result["claim_audit"]["dropped_claims"] == 0
    assert result["citation_validation_pass"] is False
    assert result["citation_reference_validation_pass"] is True
    assert result["layer_validation_pass"] is False
    assert result["validator_issue_categories"].get("layer") == 1


def test_full_count_ai_text_is_suppressed_before_validator(
    package: dict, monkeypatch
) -> None:
    raw = valid_ai_raw()
    raw["target_mechanism"][0]["text"] = "21 个独立靶点可用于治疗。"
    install_mock(monkeypatch, raw)
    result = kg07.interpret(package, "kg_llm")

    assert result["status"] == "generated"
    assert len(result["explanation"]["known_targets"]) == 1
    assert any(
        "full-count" in issue.get("issue", "")
        for issue in result["pre_validator_issues"]
    )


def test_citation_pass_is_false_but_reference_pass_true_for_language_drop(
    package: dict, monkeypatch
) -> None:
    raw = valid_ai_raw()
    raw["target_mechanism"][0]["text"] = "ABL1 可直接治愈乳腺癌。"
    raw["pathway_association"] = []
    raw["mechanism_hypotheses"] = []
    install_mock(monkeypatch, raw)
    result = kg07.interpret(package, "kg_llm")

    assert result["claim_audit"]["accepted_claims"] == 0
    assert result["claim_audit"]["dropped_claims"] >= 1
    assert result["citation_validation_pass"] is False
    assert result["citation_reference_validation_pass"] is True
    assert result["layer_validation_pass"] is True
    assert any("efficacy" in issue.get("issue", "") for issue in result["validator_issues"])


def test_interpret_rejects_mismatched_expected_build_before_provider(
    v2_package: dict, monkeypatch
) -> None:
    calls = []

    def fake_provider(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("provider must not be called for a mismatched GraphBuild")

    monkeypatch.setattr(kg07.provider, "chat_json", fake_provider)
    with pytest.raises(ValueError, match="GraphBuild"):
        kg07.interpret(
            v2_package,
            "kg_llm",
            expected_graph_build_id="GRAPH_BUILD:wrong-build",
        )
    assert calls == []


def test_rule_template_response_keeps_requested_graph_build(v2_package: dict) -> None:
    result = kg07.rule_template(
        v2_package,
        expected_graph_build_id=v2_package["graph_build_id"],
    )
    assert result["graph_build_id"] == v2_package["graph_build_id"]
    assert result["mode"] == "kg_rule"
