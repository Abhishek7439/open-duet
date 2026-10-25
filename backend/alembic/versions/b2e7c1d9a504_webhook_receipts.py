"""Durable webhook delivery receipts."""
from alembic import op
import sqlalchemy as sa

revision = "b2e7c1d9a504"
down_revision = "ac47e9b21d63"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "routine_webhook_receipt",
        sa.Column("routine_id", sa.BigInteger(), sa.ForeignKey("routine.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("routine_run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_routine_webhook_receipt_expires_at", "routine_webhook_receipt", ["expires_at"])


def downgrade():
    op.drop_table("routine_webhook_receipt")
