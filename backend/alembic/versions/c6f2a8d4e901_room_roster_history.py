"""Retain removed room memberships and their work history."""
from alembic import op
import sqlalchemy as sa

revision = "c6f2a8d4e901"
down_revision = "b2e7c1d9a504"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("room_member", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("room_member", "removed_at")
