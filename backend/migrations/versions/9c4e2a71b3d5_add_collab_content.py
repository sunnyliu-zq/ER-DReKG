"""add_collab_content

Revision ID: 9c4e2a71b3d5
Revises: 7b2d91a4c6e8
Create Date: 2026-11-20 11:00:00.000000

Adds isolated collab post / version / attachment tables. No existing business
table is altered; downgrade drops only these three new tables.
"""
from alembic import op
import sqlalchemy as sa


revision = "9c4e2a71b3d5"
down_revision = "7b2d91a4c6e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collab_posts",
        sa.Column("post_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("pert_id", sa.String(length=100), nullable=True),
        sa.Column("title", sa.String(length=250), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("author_id", sa.String(length=36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("(CURRENT_TIMESTAMP)")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("(CURRENT_TIMESTAMP)")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["collab_members.member_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("post_id"),
    )
    op.create_index(op.f("ix_collab_posts_project_id"), "collab_posts", ["project_id"], unique=False)
    op.create_index(op.f("ix_collab_posts_pert_id"), "collab_posts", ["pert_id"], unique=False)
    op.create_index(op.f("ix_collab_posts_is_deleted"), "collab_posts", ["is_deleted"], unique=False)

    op.create_table(
        "collab_post_versions",
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("post_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=250), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("change_note", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("author_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("(CURRENT_TIMESTAMP)")),
        sa.ForeignKeyConstraint(["post_id"], ["collab_posts.post_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["collab_members.member_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("version_id"),
        sa.UniqueConstraint("post_id", "version"),
    )
    op.create_index(op.f("ix_collab_post_versions_post_id"), "collab_post_versions", ["post_id"], unique=False)

    op.create_table(
        "collab_attachments",
        sa.Column("attachment_id", sa.String(length=36), nullable=False),
        sa.Column("post_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("stored_path", sa.String(length=500), nullable=False),
        sa.Column("ext", sa.String(length=16), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("file_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("uploaded_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("(CURRENT_TIMESTAMP)")),
        sa.ForeignKeyConstraint(["post_id"], ["collab_posts.post_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["collab_members.member_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("attachment_id"),
    )
    op.create_index(op.f("ix_collab_attachments_post_id"), "collab_attachments", ["post_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_collab_attachments_post_id"), table_name="collab_attachments")
    op.drop_table("collab_attachments")
    op.drop_index(op.f("ix_collab_post_versions_post_id"), table_name="collab_post_versions")
    op.drop_table("collab_post_versions")
    op.drop_index(op.f("ix_collab_posts_is_deleted"), table_name="collab_posts")
    op.drop_index(op.f("ix_collab_posts_pert_id"), table_name="collab_posts")
    op.drop_index(op.f("ix_collab_posts_project_id"), table_name="collab_posts")
    op.drop_table("collab_posts")
