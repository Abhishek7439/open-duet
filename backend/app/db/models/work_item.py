"""Personal planning records. Deadlines never enqueue agent work."""
from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, Date, ForeignKey, Identity, Index, Integer, String, Text, UniqueConstraint, false, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import BigIntPK, UTCDateTime


class WorkBoard(Base):
    __tablename__ = "work_board"
    __table_args__ = (UniqueConstraint("owner_id", "key"), {"sqlite_autoincrement": True})
    id: Mapped[int] = mapped_column(BigIntPK, Identity(always=True), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)


class WorkColumn(Base):
    __tablename__ = "work_column"
    __table_args__ = (UniqueConstraint("board_id", "key"), {"sqlite_autoincrement": True})
    id: Mapped[int] = mapped_column(BigIntPK, Identity(always=True), primary_key=True)
    board_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("work_board.id"), nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class WorkItem(Base):
    __tablename__ = "work_item"
    __table_args__ = (Index("ix_work_item_owner_board", "owner_id", "board_id"), {"sqlite_autoincrement": True})
    id: Mapped[int] = mapped_column(BigIntPK, Identity(always=True), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    board_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("work_board.id"), nullable=False)
    column_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("work_column.id"), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    priority: Mapped[str] = mapped_column(String, nullable=False, default="normal", server_default="normal")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    timezone: Mapped[str] = mapped_column(String, nullable=False, default="UTC", server_default="UTC")
    # Deliberately no FK: removing an assignment or conversation preserves the card.
    agent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    team_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    position: Mapped[int] = mapped_column(BigInteger, nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, server_default=func.now())


class WorkItemRun(Base):
    """Immutable hand-off receipt; execution lives in the existing routine runner.

    One private manual/once routine per attempt preserves its exact approved
    prompt and policy. RoutineRun remains the authority for execution state.
    """
    __tablename__ = "work_item_run"
    __table_args__ = (
        UniqueConstraint("work_item_id", "idempotency_key"),
        UniqueConstraint("work_item_id", "attempt"),
        UniqueConstraint("routine_id"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(BigIntPK, Identity(always=True), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    work_item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("work_item.id"), nullable=False)
    routine_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("routine.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, server_default=func.now())


class WorkItemEvent(Base):
    """Planner audit and execution transitions, including deduplicated reminders."""
    __tablename__ = "work_item_event"
    __table_args__ = (UniqueConstraint("work_item_id", "key"), {"sqlite_autoincrement": True})
    id: Mapped[int] = mapped_column(BigIntPK, Identity(always=True), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    work_item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("work_item.id"), nullable=False)
    receipt_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("work_item_run.id"), nullable=True)
    key: Mapped[str] = mapped_column(String, nullable=False, default=lambda: uuid4().hex)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, server_default=func.now())
