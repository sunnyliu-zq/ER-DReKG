from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]+$")
    name: str = Field(min_length=2, max_length=250)
    disease_id: str = Field(pattern=r"^(MONDO|EFO):[0-9]+$")
    status: Literal["active", "test_only"] = "active"


class ProjectOut(BaseModel):
    project_id: str
    slug: str
    name: str
    disease_id: str
    status: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class StudyCreate(BaseModel):
    accession: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    organism: str = Field(default="Homo sapiens", min_length=1, max_length=120)
    design_json: dict = Field(default_factory=dict)

    @field_validator("accession", "title", "organism")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("field must not be blank")
        return value


class StudyOut(BaseModel):
    study_id: str
    project_id: str
    accession: str
    title: str
    organism: str
    design_json: dict
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class DatasetStudyLink(BaseModel):
    study_id: str = Field(min_length=1, max_length=36)


class RunOut(BaseModel):
    analysis_run_id: str
    run_type: str
    status: str
    created_at: datetime
    parameters: dict
    model_config = ConfigDict(from_attributes=True)


class RunComparisonSide(BaseModel):
    analysis_run_id: str
    run_type: str
    status: str
    created_at: datetime
    parameters: dict
    software_versions: dict
    input_hashes: dict
    output_summary: dict


class RunComparisonOut(BaseModel):
    project_id: str
    run_type: str
    run_a: RunComparisonSide
    run_b: RunComparisonSide
    provenance_comparison: dict
    output_comparison: dict


class JobStatusOut(BaseModel):
    job_id: str
    analysis_run_id: str
    project_id: str
    state: str
    progress: int | None = None
    created_at: datetime
    description: str


class ExternalS1RankingItem(BaseModel):
    pert_id: str
    drug_name: str
    cell_id: str
    pert_type: str
    dose: str
    time: str
    n_signatures: int
    WTCS_median: float | None = None
    CC_median: float | None = None
    TAS_median: float | None = None
    external_rank: float | None = None
    B200_WTCS_median: float | None = None
    B200_rank: float | None = None


class ExternalS1ValidationOut(BaseModel):
    project_id: str
    available: bool
    reason: str | None = None
    source_dataset: str | None = None
    validation_run: str | None = None
    sample_design: dict = Field(default_factory=dict)
    summary: dict = Field(default_factory=dict)
    offset: int = 0
    limit: int = 100
    total_conditions: int = 0
    items: list[ExternalS1RankingItem] = Field(default_factory=list)
    provenance: dict = Field(default_factory=dict)


class DatasetCreate(BaseModel):
    project_id: str
    study_id: str | None = None
    source: str = Field(min_length=1)
    release: str = "unknown"
    source_url: str = ""
    sha256: str = Field(default="", pattern=r"^$|^[a-fA-F0-9]{64}$")
    license_status: str = "pending_review"
    metadata_json: dict = Field(default_factory=dict)


class PrecomputedS1Request(BaseModel):
    positive_group: str = Field(min_length=1, max_length=120)
    negative_group: str = Field(min_length=1, max_length=120)
    fdr_threshold: float = Field(default=0.05, gt=0, le=1)


class LimmaMicroarrayS1Request(BaseModel):
    sample_metadata_dataset_id: str = Field(min_length=1, max_length=100)
    positive_group: str = Field(min_length=1, max_length=120)
    negative_group: str = Field(min_length=1, max_length=120)
    identifier_type: Literal["symbol", "entrez", "ensembl"] = "symbol"
    fdr_threshold: float = Field(default=0.05, gt=0, le=1)
    measurement_type: Literal["normalized_log2_microarray"] = "normalized_log2_microarray"


class RNASeqS1Request(BaseModel):
    sample_metadata_dataset_id: str = Field(min_length=1, max_length=100)
    positive_group: str = Field(min_length=1, max_length=120)
    negative_group: str = Field(min_length=1, max_length=120)
    identifier_type: Literal["symbol", "entrez", "ensembl"] = "symbol"
    fdr_threshold: float = Field(default=0.05, gt=0, le=1)
    measurement_type: Literal["raw_integer_counts"] = "raw_integer_counts"


class S1EnrichmentRequest(BaseModel):
    s1_analysis_run_id: str = Field(min_length=20, max_length=100)
    identifier_type: Literal["symbol", "entrez", "ensembl"] = "symbol"
    fdr_threshold: float = Field(default=0.05, gt=0, le=1)


class GraphBundleRequest(BaseModel):
    s1_analysis_run_id: str = Field(min_length=20, max_length=100)
    s2_analysis_run_id: str = Field(min_length=20, max_length=100)
    enrichment_analysis_run_id: str = Field(min_length=20, max_length=100)


class S2ImportRequest(BaseModel):
    analysis_run_id: str = Field(min_length=20, max_length=100)


class S2RunRequest(BaseModel):
    s1_analysis_run_id: str = Field(min_length=20, max_length=100)
    identifier_type: Literal["symbol", "entrez", "ensembl"] = "symbol"
    fdr_threshold: float = Field(default=0.05, gt=0, le=1)
    top_n: int = Field(default=100, ge=1, le=500)
    cell_id: str = Field(default="MCF7", min_length=1, max_length=40)
    perturbation_type: str = Field(default="trt_cp", min_length=1, max_length=40)
    time: str = Field(default="24 h", min_length=1, max_length=40)
    min_distil_cc_q75: float = Field(default=0.2, ge=0, le=1)
    max_signatures: int | None = Field(default=None, ge=1, le=100)


class DatasetOut(BaseModel):
    dataset_id: str
    study_id: str | None = None
    source: str
    release: str
    sha256: str
    state: str
    license_status: str
    model_config = ConfigDict(from_attributes=True)


class GraphNode(BaseModel):
    id: str
    type: str
    label: str
    properties: dict


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    relation: str
    evidence_ids: list[str]


class SubgraphOut(BaseModel):
    project_id: str
    graph_build_id: str | None = None
    analysis_run_id: str | None = None
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool
    next_cursor: str | None = None


class KG06CoverageOut(BaseModel):
    project_id: str
    graph_build_id: str
    audit_status: str
    coverage: dict
    items: list[dict]
    input_hashes: dict


class PostCreate(BaseModel):
    pert_id: str | None = Field(default=None, min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=250)
    summary_text: str = Field(default="", max_length=20000)
    change_note: str = Field(default="", max_length=500)


class PostUpdate(BaseModel):
    pert_id: str | None = Field(default=None, min_length=1, max_length=100)
    title: str | None = Field(default=None, min_length=1, max_length=250)
    summary_text: str | None = Field(default=None, max_length=20000)
    change_note: str = Field(default="", max_length=500)


class AttachmentOut(BaseModel):
    attachment_id: str
    post_id: str
    kind: str
    original_filename: str
    ext: str
    mime_type: str
    file_bytes: int
    sha256: str
    uploaded_by: str | None = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class VersionOut(BaseModel):
    version_id: str
    version: int
    title: str
    summary_text: str
    change_note: str
    author_id: str | None = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class PostSummaryOut(BaseModel):
    post_id: str
    project_id: str
    pert_id: str | None = None
    title: str
    current_version: int
    author_id: str | None = None
    author_name: str = ""
    attachment_count: int = 0
    created_at: datetime
    updated_at: datetime


class PostDetailOut(PostSummaryOut):
    summary_text: str
    attachments: list[AttachmentOut] = Field(default_factory=list)
    versions: list[VersionOut] = Field(default_factory=list)


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class MemberOut(BaseModel):
    member_id: str
    username: str
    display_name: str
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None
    model_config = ConfigDict(from_attributes=True)


class LoginResult(BaseModel):
    access_token: str
    token_type: str = "bearer"
    member: MemberOut


class KG06EvidencePackageOut(BaseModel):
    package_version: str
    package_id: str
    generated_at_utc: str
    package_sha256: str
    project_id: str
    project_slug: str
    graph_build_id: str
    analysis_run_ids: dict[str, str]
    pert_id: str
    candidate: dict
    screening_evidence: dict
    paths: list[dict]
    evidence_gaps: list[str]
    coverage: dict
    provenance: dict
