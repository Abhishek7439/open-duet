"""Planner-to-executor boundary: durable receipts, races and real fake-CLI turns."""
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.core.config import settings
from app.db.models import Agent, Notification, Routine, RoutineRun, WorkItem, WorkItemRun, WorkItemEvent, WorkColumn, WorkBoard, Thread, Turn
from app.schemas.work_item import WorkRunCreate
from app.services import work_execution as execution, work_planner, routine_service as routines
from tests.services.thread_harness import cleanup, cli, owner, rt, sessions, tools_on, wait_until  # noqa: F401


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_EXECUTION_ENABLED", True)


@pytest.fixture(autouse=True)
async def planner_cleanup(sessions, owner):
    yield
    async with sessions() as db:
        await db.execute(delete(WorkItemEvent).where(WorkItemEvent.owner_id == owner))
        await db.execute(delete(WorkItemRun).where(WorkItemRun.owner_id == owner))
        await db.execute(delete(WorkItem).where(WorkItem.owner_id == owner))
        await db.execute(delete(WorkColumn).where(WorkColumn.board_id.in_(select(WorkBoard.id).where(WorkBoard.owner_id == owner))))
        await db.execute(delete(WorkBoard).where(WorkBoard.owner_id == owner))
        await db.execute(delete(RoutineRun).where(RoutineRun.routine_id.in_(select(Routine.id).where(Routine.owner_id == owner))))
        await db.execute(delete(Routine).where(Routine.owner_id == owner))
        await db.execute(delete(Agent).where(Agent.owner_id == owner))
        await db.commit()


@pytest.fixture
async def card(sessions, owner, tmp_path):
    async with sessions() as db:
        agent = Agent(owner_id=owner, name="Writer", workspace_path=str(tmp_path))
        db.add(agent)
        await db.commit()
        board = await work_planner.default_board(db, owner)
        column = await db.scalar(select(WorkColumn).where(WorkColumn.board_id == board.id, WorkColumn.key == "ready"))
        item = await work_planner.create_item(db, owner, dict(board_id=board.id, column_id=column.id,
            title="Prepare report", description="Use the sources", agent_id=agent.id))
        return item


def payload(card, **changes):
    return WorkRunCreate(revision=card.revision, idempotency_key=uuid4().hex,
                         agent_id=card.agent_id, prompt="Prepare report from the sources", **changes)


async def start(sessions, owner, card, data=None):
    async with sessions() as db:
        return await execution.start(db, owner, card.id, data or payload(card))


async def latest(sessions, owner, card):
    async with sessions() as db:
        return (await execution.list_runs(db, owner, card.id))[0]


async def test_duplicate_clicks_and_lost_response_return_one_receipt(sessions, owner, card):
    request = payload(card)
    results = await asyncio.gather(*(start(sessions, owner, card, request) for _ in range(3)))
    assert len({run.id for run in results}) == 1
    async with sessions() as db:
        assert len((await db.scalars(select(RoutineRun).join(Routine).where(Routine.owner_id == owner))).all()) == 1
        assert await routines.list_routines(db, owner) == []
        with pytest.raises(routines.RoutineNotFound):
            await routines.run_now(db, owner, results[0].routine_id)


async def test_separate_requests_cannot_start_same_card_twice(sessions, owner, card):
    results = await asyncio.gather(start(sessions, owner, card), start(sessions, owner, card), return_exceptions=True)
    assert sum(isinstance(value, HTTPException) and value.status_code == 409 for value in results) == 1


async def test_scope_revision_archived_and_schedule_validation(sessions, owner, card):
    with pytest.raises(HTTPException) as foreign:
        await start(sessions, "someone-else", card)
    assert foreign.value.status_code == 404
    with pytest.raises(HTTPException) as past:
        await start(sessions, owner, card, payload(card, scheduled_for=datetime.now(UTC) - timedelta(days=1)))
    assert past.value.status_code == 422
    async with sessions() as db:
        await work_planner.update_item(db, owner, card.id, card.revision, {"archived": True})
    with pytest.raises(HTTPException) as archived:
        await start(sessions, owner, card)
    assert archived.value.status_code == 409


async def test_complete_task_links_before_send_and_requires_human_acceptance(sessions, owner, card, rt, cli, monkeypatch):
    request = payload(card)
    receipt = await start(sessions, owner, card, request)
    original = rt.send_turn
    async def checked_send(thread_id, *args, **kwargs):
        saved = await latest(sessions, owner, card)
        assert saved.thread_id == thread_id
        assert saved.state == "claimed"
        return await original(thread_id, *args, **kwargs)
    monkeypatch.setattr(rt, "send_turn", checked_send)
    scheduler = routines.RoutineScheduler(rt)
    await scheduler.tick()
    async def finished():
        await scheduler.tick()
        return (await latest(sessions, owner, card)).state == "completed"
    await wait_until(finished)
    saved = await latest(sessions, owner, card)
    assert saved.id == receipt.id and saved.result and saved.thread_id
    async with sessions() as db:
        current = await db.get(WorkItem, card.id)
        assert current.column_id == card.column_id and current.completed_at is None
        events = (await db.scalars(select(WorkItemEvent.kind).where(WorkItemEvent.receipt_id == receipt.id)
                                  .order_by(WorkItemEvent.id))).all()
        assert events == ["queued", "claimed", "thread_linked", "started", "completed"]
    # A network retry after completion still returns the original receipt.
    assert (await start(sessions, owner, card, request)).id == receipt.id
    assert len(cli.launches()) == 1


async def test_busy_agent_queues_then_starts_after_release(sessions, owner, card, rt, cli):
    async with sessions() as db:
        thread = Thread(owner_id=owner, provider="claude", agent_id=card.agent_id)
        db.add(thread)
        await db.flush()
        turn = Turn(thread_id=thread.id, seq=1, prompt_text="Other work", status="running")
        db.add(turn)
        await db.commit()
    await start(sessions, owner, card)
    scheduler = routines.RoutineScheduler(rt)
    await scheduler.tick()
    assert (await latest(sessions, owner, card)).state == "queued"
    assert not cli.launches()
    async with sessions() as db:
        persisted = await db.get(Turn, turn.id)
        persisted.status = "succeeded"
        await db.commit()
    await scheduler.tick()
    assert (await latest(sessions, owner, card)).thread_id is not None


async def test_once_delivery_cancel_and_recovery_do_not_replay(sessions, owner, card, rt, cli):
    now = datetime.now(UTC)
    receipt = await start(sessions, owner, card, payload(card, scheduled_for=now + timedelta(minutes=5)))
    scheduler = routines.RoutineScheduler(rt, clock=lambda: now)
    await scheduler.tick()
    assert (await latest(sessions, owner, card)).state == "scheduled"
    cancelled = await execution.cancel(scheduler, owner, card.id, receipt.id)
    assert cancelled.state == "cancelled"
    assert (await execution.cancel(scheduler, owner, card.id, receipt.id)).state == "cancelled"
    scheduler.clock = lambda: now + timedelta(minutes=6)
    await scheduler.tick()
    assert not cli.launches()
    cli.script({"type": "hang"})
    second = await start(sessions, owner, card)
    assert second.attempt == 2
    await scheduler.tick()
    await wait_until(lambda: cli.launches())
    await rt.shutdown()
    recovered = routines.RoutineScheduler(rt)
    await recovered.recover()
    assert (await latest(sessions, owner, card)).state == "interrupted"
    await recovered.tick()
    assert len(cli.launches()) == 1


async def test_stop_running_preserves_history_and_retry_is_explicit(sessions, owner, card, rt, cli):
    cli.script({"type": "hang"})
    run = await start(sessions, owner, card)
    scheduler = routines.RoutineScheduler(rt)
    await scheduler.tick()
    await wait_until(lambda: cli.launches())
    stopped = await execution.cancel(scheduler, owner, card.id, run.id)
    assert stopped.state == "cancelled"
    await scheduler.tick()
    assert (await latest(sessions, owner, card)).state == "cancelled"
    async with sessions() as db:
        assert len(await execution.list_runs(db, owner, card.id)) == 1


async def test_due_reminders_are_deduplicated_and_never_launch(sessions, owner, card, rt, cli):
    now = datetime.now(UTC)
    async with sessions() as db:
        await work_planner.update_item(db, owner, card.id, card.revision, {"due_at": now - timedelta(minutes=1)})
    scheduler = routines.RoutineScheduler(rt, clock=lambda: now)
    await scheduler.tick()
    await scheduler.tick()
    assert not cli.launches()
    async with sessions() as db:
        notices = (await db.scalars(select(Notification).where(Notification.owner_id == owner, Notification.kind == "task_due"))).all()
        assert len(notices) == 1
        assert not await db.scalar(select(WorkItemRun.id).where(WorkItemRun.work_item_id == card.id))


async def test_one_time_schedule_fires_once_and_links_the_card(sessions, owner, card, rt, cli):
    now = datetime.now(UTC)
    at = now + timedelta(minutes=5)
    await start(sessions, owner, card, payload(card, scheduled_for=at))
    scheduler = routines.RoutineScheduler(rt, clock=lambda: at)
    await scheduler.tick()
    async def completed():
        await scheduler.tick()
        return (await latest(sessions, owner, card)).state == "completed"
    await wait_until(completed)
    await scheduler.tick()
    await scheduler.tick()
    assert len(cli.launches()) == 1
    async with sessions() as db:
        annotated = (await execution.with_status(db, owner, [await db.get(WorkItem, card.id)]))[0]
        assert annotated.execution_state == "completed"
        assert annotated.execution_thread_id is not None
        assert annotated.execution_attempt == 1


async def test_task_waiting_for_approval_has_visible_receipt(sessions, owner, card, rt, cli, tools_on):
    cli.script({"type": "tool", "tool_name": "Bash", "tool_input": {"command": "git status -s"}})
    await start(sessions, owner, card)
    scheduler = routines.RoutineScheduler(rt)
    await scheduler.tick()
    async def waiting():
        await scheduler.tick()
        return (await latest(sessions, owner, card)).state == "needs_input"
    await wait_until(waiting)
    receipt = await latest(sessions, owner, card)
    assert receipt.thread_id
    async with sessions() as db:
        assert await db.scalar(select(WorkItemEvent.id).where(
            WorkItemEvent.receipt_id == receipt.id, WorkItemEvent.kind == "needs_input"))
    assert (await execution.cancel(scheduler, owner, card.id, receipt.id)).state == "cancelled"
