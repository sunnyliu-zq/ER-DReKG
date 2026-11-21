"""Collaboration-center routes.

Isolated from the research KG APIs: these endpoints only touch collab_*
tables and collab upload storage. Every content route requires an authenticated
member; login itself is the only unauthenticated endpoint.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.collab_auth import create_access_token, get_current_member, verify_password
from app.collab_service import (
    _ensure_project,
    can_edit,
    create_post,
    delete_post,
    member_name,
    store_attachment,
    update_post,
    attachment_abs_path,
)
from app.db import get_db
from app.models import CollabAttachment, CollabMember, CollabPost, CollabPostVersion
from app.schemas import (
    AttachmentOut,
    LoginRequest,
    LoginResult,
    MemberOut,
    PostCreate,
    PostDetailOut,
    PostSummaryOut,
    PostUpdate,
    VersionOut,
)

router = APIRouter(prefix="/api/v1/collab", tags=["collab"])

_INVALID = HTTPException(status_code=401, detail="用户名或口令不正确", headers={"WWW-Authenticate": "Bearer"})
_FORBIDDEN = HTTPException(status_code=403, detail="仅成果作者或管理员可修改或删除")


@router.post("/auth/login", response_model=LoginResult)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResult:
    member = db.scalar(select(CollabMember).where(CollabMember.username == payload.username.strip().lower()))
    if member is None or not member.is_active or not verify_password(payload.password, member.password_hash):
        raise _INVALID
    member.last_login_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(member)
    return LoginResult(access_token=create_access_token(member), member=MemberOut.model_validate(member))


@router.get("/auth/me", response_model=MemberOut)
def me(current: CollabMember = Depends(get_current_member)) -> CollabMember:
    return current


def _get_post_or_404(db: Session, project_id: str, post_id: str) -> CollabPost:
    post = db.get(CollabPost, post_id)
    if post is None or post.project_id != project_id or post.is_deleted:
        raise HTTPException(404, "成果不存在")
    return post


def _summary(db: Session, post: CollabPost) -> PostSummaryOut:
    count = db.scalar(select(func.count()).select_from(CollabAttachment).where(CollabAttachment.post_id == post.post_id))
    return PostSummaryOut(
        post_id=post.post_id,
        project_id=post.project_id,
        pert_id=post.pert_id,
        title=post.title,
        current_version=post.current_version,
        author_id=post.author_id,
        author_name=member_name(db, post.author_id),
        attachment_count=count or 0,
        created_at=post.created_at,
        updated_at=post.updated_at,
    )


@router.get("/projects/{project_id}/posts", response_model=list[PostSummaryOut])
def list_posts(
    project_id: str,
    pert_id: str | None = None,
    keyword: str | None = None,
    db: Session = Depends(get_db),
    current: CollabMember = Depends(get_current_member),
) -> list[PostSummaryOut]:
    _ensure_project(db, project_id)
    stmt = select(CollabPost).where(CollabPost.project_id == project_id, CollabPost.is_deleted.is_(False))
    if pert_id:
        stmt = stmt.where(CollabPost.pert_id == pert_id)
    if keyword:
        kw = f"%{keyword.lower()}%"
        stmt = stmt.where(or_(func.lower(CollabPost.title).like(kw), func.lower(CollabPost.summary_text).like(kw)))
    stmt = stmt.order_by(CollabPost.updated_at.desc())
    posts = db.scalars(stmt).all()
    return [_summary(db, post) for post in posts]


@router.post("/projects/{project_id}/posts", response_model=PostDetailOut, status_code=201)
def create_post_route(
    project_id: str,
    payload: PostCreate,
    db: Session = Depends(get_db),
    current: CollabMember = Depends(get_current_member),
) -> PostDetailOut:
    post = create_post(db, project_id, current, payload)
    return _detail(db, post)


@router.get("/projects/{project_id}/posts/{post_id}", response_model=PostDetailOut)
def get_post(
    project_id: str,
    post_id: str,
    db: Session = Depends(get_db),
    current: CollabMember = Depends(get_current_member),
) -> PostDetailOut:
    _ensure_project(db, project_id)
    post = _get_post_or_404(db, project_id, post_id)
    return _detail(db, post)


@router.patch("/projects/{project_id}/posts/{post_id}", response_model=PostDetailOut)
def update_post_route(
    project_id: str,
    post_id: str,
    payload: PostUpdate,
    db: Session = Depends(get_db),
    current: CollabMember = Depends(get_current_member),
) -> PostDetailOut:
    _ensure_project(db, project_id)
    post = _get_post_or_404(db, project_id, post_id)
    if not can_edit(current, post):
        raise _FORBIDDEN
    update_post(db, post, current, payload)
    return _detail(db, post)


@router.post("/projects/{project_id}/posts/{post_id}/attachments", response_model=AttachmentOut, status_code=201)
async def upload_attachment(
    project_id: str,
    post_id: str,
    kind: str,
    request: Request,
    filename: str,
    db: Session = Depends(get_db),
    current: CollabMember = Depends(get_current_member),
) -> AttachmentOut:
    _ensure_project(db, project_id)
    post = _get_post_or_404(db, project_id, post_id)
    if not can_edit(current, post):
        raise _FORBIDDEN
    data = await request.body()
    attachment = store_attachment(db, post, current, kind, filename, data)
    return AttachmentOut.model_validate(attachment)


@router.get("/attachments/{attachment_id}/download")
def download_attachment(
    attachment_id: str,
    db: Session = Depends(get_db),
    current: CollabMember = Depends(get_current_member),
) -> FileResponse:
    attachment = db.get(CollabAttachment, attachment_id)
    if attachment is None:
        raise HTTPException(404, "附件不存在")
    post = db.get(CollabPost, attachment.post_id)
    if post is None or post.is_deleted:
        raise HTTPException(404, "附件不存在")
    abs_path = attachment_abs_path(attachment)
    if not abs_path.is_file():
        raise HTTPException(404, "附件文件缺失")
    return FileResponse(abs_path, media_type=attachment.mime_type, filename=attachment.original_filename)


@router.delete("/projects/{project_id}/posts/{post_id}", status_code=204)
def delete_post_route(
    project_id: str,
    post_id: str,
    db: Session = Depends(get_db),
    current: CollabMember = Depends(get_current_member),
) -> None:
    _ensure_project(db, project_id)
    post = _get_post_or_404(db, project_id, post_id)
    if not can_edit(current, post):
        raise _FORBIDDEN
    delete_post(db, post)


def _detail(db: Session, post: CollabPost) -> PostDetailOut:
    summary = _summary(db, post)
    attachments = db.scalars(select(CollabAttachment).where(CollabAttachment.post_id == post.post_id).order_by(CollabAttachment.created_at)).all()
    versions = db.scalars(select(CollabPostVersion).where(CollabPostVersion.post_id == post.post_id).order_by(CollabPostVersion.version)).all()
    return PostDetailOut(
        **summary.model_dump(),
        summary_text=post.summary_text,
        attachments=[AttachmentOut.model_validate(a) for a in attachments],
        versions=[VersionOut.model_validate(v) for v in versions],
    )
