"""Business logic for the collaboration center.

All functions operate only on collab_* tables and the isolated collab upload
directory. The research KG (Neo4j) and existing business tables are not touched.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT, settings
from app.models import Candidate, CollabAttachment, CollabMember, CollabPost, CollabPostVersion, Project
from app.schemas import PostCreate, PostUpdate

# kind -> {extension: mime_type}
ALLOWED_TYPES: dict[str, dict[str, str]] = {
    "doc": {".md": "text/markdown", ".txt": "text/plain"},
    "data": {".csv": "text/csv", ".tsv": "text/tab-separated-values", ".json": "application/json"},
    "figure": {".png": "image/png", ".svg": "image/svg+xml"},
}
MAX_BYTES_BY_KIND = {
    "doc": lambda: settings.collab_doc_max_bytes,
    "data": lambda: settings.collab_data_max_bytes,
    "figure": lambda: settings.collab_figure_max_bytes,
}
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_SVG_DANGEROUS = re.compile(r"<script|on\w+\s*=|javascript:", re.IGNORECASE)


def _collab_root() -> Path:
    root = Path(settings.collab_upload_dir)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root


def _clean_filename(filename: str) -> str:
    return PurePosixPath(filename.replace("\\", "/")).name.strip()


def _validate_content(kind: str, ext: str, data: bytes) -> str:
    """Return the validated mime type; raise HTTPException on forbidden content."""
    allowed = ALLOWED_TYPES[kind]
    mime_type = allowed[ext]

    if ext == ".png":
        if not data.startswith(_PNG_MAGIC):
            raise HTTPException(422, "PNG 文件头校验失败")
        return mime_type

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "文件不是有效的 UTF-8 文本") from exc

    if ext == ".json":
        try:
            json.loads(text)
        except ValueError as exc:
            raise HTTPException(422, "JSON 解析失败") from exc
        return mime_type

    if ext == ".svg":
        if _SVG_DANGEROUS.search(text):
            raise HTTPException(422, "SVG 含脚本或事件属性，已拒绝")
        return mime_type

    # .md / .txt / .csv / .tsv: plain text, non-empty after strip check by caller.
    return mime_type


def validate_attachment(kind: str, filename: str, data: bytes) -> tuple[str, str, int, str]:
    """Validate kind/extension/size/content. Returns (ext, mime_type, size, sha256)."""
    if kind not in ALLOWED_TYPES:
        raise HTTPException(422, "附件类型非法")
    safe_name = _clean_filename(filename)
    if not safe_name:
        raise HTTPException(422, "文件名为空")
    ext = Path(safe_name).suffix.lower()
    if ext not in ALLOWED_TYPES[kind]:
        raise HTTPException(415, f"该类型不允许上传 {ext} 文件")

    max_bytes = MAX_BYTES_BY_KIND[kind]()
    if len(data) == 0:
        raise HTTPException(422, "上传文件为空")
    if len(data) > max_bytes:
        raise HTTPException(413, f"文件超过 {max_bytes} 字节上限")

    mime_type = _validate_content(kind, ext, data)
    sha256 = hashlib.sha256(data).hexdigest()
    return ext, mime_type, len(data), sha256


def _ensure_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


def _verify_pert_id(db: Session, project_id: str, pert_id: str | None) -> None:
    if pert_id is None:
        return
    exists = db.scalar(
        select(Candidate.candidate_id).where(Candidate.project_id == project_id, Candidate.pert_id == pert_id).limit(1)
    )
    if exists is None:
        raise HTTPException(422, "pert_id 不属于本项目候选药物")


def can_edit(member: CollabMember, post: CollabPost) -> bool:
    return member.role == "admin" or post.author_id == member.member_id


def create_post(db: Session, project_id: str, member: CollabMember, payload: PostCreate) -> CollabPost:
    _ensure_project(db, project_id)
    _verify_pert_id(db, project_id, payload.pert_id)
    post = CollabPost(
        project_id=project_id,
        pert_id=payload.pert_id,
        title=payload.title.strip(),
        summary_text=payload.summary_text.strip(),
        author_id=member.member_id,
        current_version=1,
    )
    db.add(post)
    db.flush()
    version = CollabPostVersion(
        post_id=post.post_id,
        version=1,
        title=post.title,
        summary_text=post.summary_text,
        change_note=payload.change_note.strip(),
        author_id=member.member_id,
    )
    db.add(version)
    db.commit()
    db.refresh(post)
    return post


def update_post(db: Session, post: CollabPost, member: CollabMember, payload: PostUpdate) -> CollabPost:
    if payload.pert_id is not None:
        _verify_pert_id(db, post.project_id, payload.pert_id)
        post.pert_id = payload.pert_id
    if payload.title is not None:
        post.title = payload.title.strip()
    if payload.summary_text is not None:
        post.summary_text = payload.summary_text.strip()

    new_version = post.current_version + 1
    version = CollabPostVersion(
        post_id=post.post_id,
        version=new_version,
        title=post.title,
        summary_text=post.summary_text,
        change_note=payload.change_note.strip(),
        author_id=member.member_id,
    )
    db.add(version)
    post.current_version = new_version
    post.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(post)
    return post


def store_attachment(
    db: Session, post: CollabPost, member: CollabMember, kind: str, filename: str, data: bytes
) -> CollabAttachment:
    ext, mime_type, size, sha256 = validate_attachment(kind, filename, data)
    root = _collab_root()
    directory = root / post.project_id / post.post_id
    directory.mkdir(parents=True, exist_ok=True)
    attachment_id = str(uuid4())
    stored_path = directory / f"{attachment_id}{ext}"
    rel_path = stored_path.relative_to(root).as_posix()

    attachment = CollabAttachment(
        attachment_id=attachment_id,
        post_id=post.post_id,
        kind=kind,
        original_filename=_clean_filename(filename),
        stored_path=rel_path,
        ext=ext,
        mime_type=mime_type,
        file_bytes=size,
        sha256=sha256,
        uploaded_by=member.member_id,
    )
    try:
        stored_path.write_bytes(data)
        db.add(attachment)
        db.commit()
        db.refresh(attachment)
    except Exception:
        # Keep DB and disk consistent: remove a partially written file on failure.
        stored_path.unlink(missing_ok=True)
        db.rollback()
        raise
    return attachment


def attachment_abs_path(attachment: CollabAttachment) -> Path:
    root = _collab_root()
    abs_path = (root / attachment.stored_path).resolve()
    if not abs_path.is_relative_to(root.resolve()):
        raise HTTPException(400, "附件路径非法")
    return abs_path


def delete_post(db: Session, post: CollabPost) -> None:
    # Soft delete only: history and files are retained for traceability.
    post.is_deleted = True
    post.updated_at = datetime.now(timezone.utc)
    db.commit()


def member_name(db: Session, member_id: str | None) -> str:
    if member_id is None:
        return ""
    member = db.get(CollabMember, member_id)
    return member.display_name if member else ""
