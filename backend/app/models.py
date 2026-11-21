from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def now_utc():
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"
    project_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(250))
    disease_id: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(40), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    runs: Mapped[list["AnalysisRun"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    datasets: Mapped[list["Dataset"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    studies: Mapped[list["Study"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Study(Base):
    __tablename__ = "studies"
    __table_args__ = (UniqueConstraint("project_id", "accession"),)
    study_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), index=True)
    accession: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(500))
    organism: Mapped[str] = mapped_column(String(120), default="Homo sapiens")
    design_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    project: Mapped[Project] = relationship(back_populates="studies")
    datasets: Mapped[list["Dataset"]] = relationship(back_populates="study")


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    analysis_run_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), index=True)
    run_type: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(40), default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    software_versions: Mapped[dict] = mapped_column(JSON, default=dict)
    input_hashes: Mapped[dict] = mapped_column(JSON, default=dict)
    project: Mapped[Project] = relationship(back_populates="runs")


class GraphBuild(Base):
    """Immutable record of a graph snapshot materialized from audited inputs."""
    __tablename__ = "graph_builds"
    __table_args__ = (UniqueConstraint("project_id", "build_key"),)
    graph_build_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), index=True)
    analysis_run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.analysis_run_id", ondelete="CASCADE"), index=True)
    build_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), default="materialized_snapshot")
    graph_source: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())
    input_hashes: Mapped[dict] = mapped_column(JSON, default=dict)
    summary_json: Mapped[dict] = mapped_column(JSON, default=dict)


class EvidenceRecord(Base):
    """Immutable source assertion copied from a verified GraphBundle per graph version."""
    __tablename__ = "evidence_records"
    __table_args__ = (
        UniqueConstraint("graph_build_id", "evidence_id"),
    )
    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    graph_build_id: Mapped[str] = mapped_column(ForeignKey("graph_builds.graph_build_id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), index=True)
    evidence_id: Mapped[str] = mapped_column(String(64), index=True)
    source_release: Mapped[str] = mapped_column(String(250))
    source_record_id: Mapped[str] = mapped_column(Text, default="")
    subject_id: Mapped[str] = mapped_column(Text)
    predicate: Mapped[str] = mapped_column(String(120))
    object_id: Mapped[str] = mapped_column(Text)
    source_assertion: Mapped[dict] = mapped_column(JSON)
    source_url: Mapped[object | None] = mapped_column(JSON, nullable=True)
    assertion_sha256: Mapped[str] = mapped_column(String(64))


class DiseaseContext(Base):
    """Disease-specific expression context immutable within each graph version."""
    __tablename__ = "disease_contexts"
    __table_args__ = (UniqueConstraint("graph_build_id", "context_id"),)
    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    graph_build_id: Mapped[str] = mapped_column(ForeignKey("graph_builds.graph_build_id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), index=True)
    context_id: Mapped[str] = mapped_column(Text, index=True)
    disease_id: Mapped[str] = mapped_column(Text, default="")
    context_label: Mapped[str] = mapped_column(Text, default="")
    context_json: Mapped[dict] = mapped_column(JSON)
    context_sha256: Mapped[str] = mapped_column(String(64))


class Dataset(Base):
    __tablename__ = "datasets"
    dataset_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), index=True)
    study_id: Mapped[str | None] = mapped_column(ForeignKey("studies.study_id", ondelete="SET NULL"), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(250))
    release: Mapped[str] = mapped_column(String(100), default="unknown")
    source_url: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str] = mapped_column(String(64), default="")
    license_status: Mapped[str] = mapped_column(String(100), default="pending_review")
    state: Mapped[str] = mapped_column(String(40), default="validation_pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    project: Mapped[Project] = relationship(back_populates="datasets")
    study: Mapped[Study | None] = relationship(back_populates="datasets")


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (UniqueConstraint("project_id", "analysis_run_id", "pert_id"),)
    candidate_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), index=True)
    analysis_run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.analysis_run_id", ondelete="CASCADE"), index=True)
    pert_id: Mapped[str] = mapped_column(String(100), index=True)
    drug_name: Mapped[str] = mapped_column(String(250))
    rank: Mapped[int] = mapped_column(Integer)
    score_name: Mapped[str] = mapped_column(String(50), default="WTCS")
    score_value: Mapped[str] = mapped_column(String(40), default="")
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)


class S2ConditionResult(Base):
    """Immutable drug-condition scores tied to one project S2 AnalysisRun."""
    __tablename__ = "s2_condition_results"
    __table_args__ = (UniqueConstraint("analysis_run_id", "condition_key", "pert_id"),)
    result_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), index=True)
    analysis_run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.analysis_run_id", ondelete="CASCADE"), index=True)
    pert_id: Mapped[str] = mapped_column(String(100), index=True)
    condition_key: Mapped[str] = mapped_column(String(500), index=True)
    rank_within_condition: Mapped[float] = mapped_column(Float)
    condition_json: Mapped[dict] = mapped_column(JSON, default=dict)


class CollabMember(Base):
    """Collaboration-center member. Independent from the research KG data."""
    __tablename__ = "collab_members"
    member_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20), default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CollabPost(Base):
    """A collaborative explanation entry, linked softly to a candidate drug by (project_id, pert_id)."""
    __tablename__ = "collab_posts"
    post_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), index=True)
    pert_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(250))
    summary_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    author_id: Mapped[str | None] = mapped_column(ForeignKey("collab_members.member_id", ondelete="SET NULL"), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())


class CollabPostVersion(Base):
    """Immutable snapshot of a post at a point in time; history is never overwritten."""
    __tablename__ = "collab_post_versions"
    __table_args__ = (UniqueConstraint("post_id", "version"),)
    version_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    post_id: Mapped[str] = mapped_column(ForeignKey("collab_posts.post_id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(250))
    summary_text: Mapped[str] = mapped_column(Text, default="")
    change_note: Mapped[str] = mapped_column(String(500), default="")
    author_id: Mapped[str | None] = mapped_column(ForeignKey("collab_members.member_id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())


class CollabAttachment(Base):
    """Metadata for an uploaded collab file stored under the isolated collab upload directory."""
    __tablename__ = "collab_attachments"
    attachment_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    post_id: Mapped[str] = mapped_column(ForeignKey("collab_posts.post_id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    ext: Mapped[str] = mapped_column(String(16))
    mime_type: Mapped[str] = mapped_column(String(100))
    file_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_by: Mapped[str | None] = mapped_column(ForeignKey("collab_members.member_id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())
