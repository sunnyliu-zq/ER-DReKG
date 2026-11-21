"""add_collab_member

Revision ID: 7b2d91a4c6e8
Revises: e4f62ab7093c
Create Date: 2026-11-20 10:00:00.000000

Adds the isolated collaboration-center member table. No existing table is
altered; downgrade drops only this new table.
"""
from alembic import op
import sqlalchemy as sa


revision = "7b2d91a4c6e8"
down_revision = "e4f62ab7093c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collab_members",
        sa.Column("member_id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="member"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("(CURRENT_TIMESTAMP)")),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("member_id"),
    )
    op.create_index(op.f("ix_collab_members_username"), "collab_members", ["username"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_collab_members_username"), table_name="collab_members")
    op.drop_table("collab_members")
