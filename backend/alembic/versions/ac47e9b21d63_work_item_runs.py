"""Explicit task execution receipts, using the routine executor."""
from alembic import op
import sqlalchemy as sa

revision = "ac47e9b21d63"
down_revision = "9e6d3b1a7c52"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("work_item_run",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(always=True), primary_key=True),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("work_item_id", sa.BigInteger(), sa.ForeignKey("work_item.id"), nullable=False),
        sa.Column("routine_id", sa.BigInteger(), sa.ForeignKey("routine.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("work_item_id", "idempotency_key"),
        sa.UniqueConstraint("work_item_id", "attempt"),
        sa.UniqueConstraint("routine_id"), sqlite_autoincrement=True)
    op.create_table("work_item_event",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(always=True), primary_key=True),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("work_item_id", sa.BigInteger(), sa.ForeignKey("work_item.id"), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), sa.ForeignKey("work_item_run.id"), nullable=True),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("work_item_id", "key"), sqlite_autoincrement=True)


def downgrade():
    op.drop_table("work_item_event")
    op.drop_table("work_item_run")
