"""Explicit planner hand-offs. Never called by assignment or deadline edits."""
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.db.models import Routine, RoutineRun, WorkItem, WorkItemRun
from app.db.models.routine import ACTIVE_RUN_STATES
from app.db.transactions import begin_write
from app.schemas.work_item import WorkRunResponse, WorkResponse
from app.services import routine_service as routines
from app.services import work_events
from app.services.work_planner import error


async def owned_item(db, owner, item_id, *, lock=False):
    query = select(WorkItem).where(WorkItem.id == item_id, WorkItem.owner_id == owner)
    if lock:
        query = query.with_for_update(key_share=True)
    item = await db.scalar(query)
    if item is None:
        raise error(404, "Task not found")
    return item


async def describe(db, receipt):
    routine = await db.get(Routine, receipt.routine_id)
    run = await db.scalar(select(RoutineRun).where(RoutineRun.routine_id == routine.id)
                          .order_by(RoutineRun.id.desc()).limit(1))
    state = execution_state(receipt, run)
    return WorkRunResponse(
        id=receipt.id, work_item_id=receipt.work_item_id, attempt=receipt.attempt,
        routine_id=routine.id, run_id=run.id if run else None, agent_id=routine.agent_id,
        prompt=routine.prompt, approval_mode=routine.approval_mode, state=state,
        scheduled_for=run.scheduled_for if run else datetime.fromisoformat(routine.schedule_spec["at"]),
        thread_id=run.thread_id if run else None, result=run.result if run else None,
        error=run.error if run else None, started_at=run.started_at if run else None,
        finished_at=receipt.cancelled_at or (run.finished_at if run else None), created_at=receipt.created_at,
    )


def execution_state(receipt, run):
    if receipt.cancelled_at or (run and run.error == "The run was stopped."):
        return "cancelled"
    if run is None:
        return "scheduled"
    if run.state == "running" and run.turn_id is None:
        return "claimed"
    return run.state


async def list_runs(db, owner, item_id):
    await owned_item(db, owner, item_id)
    receipts = (await db.scalars(select(WorkItemRun).where(
        WorkItemRun.owner_id == owner, WorkItemRun.work_item_id == item_id)
        .order_by(WorkItemRun.attempt.desc()))).all()
    return [await describe(db, receipt) for receipt in receipts]


async def with_status(db, owner, items):
    """Batch the latest receipt into board/calendar cards without full history."""
    latest_ids = select(func.max(WorkItemRun.id)).where(
        WorkItemRun.owner_id == owner, WorkItemRun.work_item_id.in_([item.id for item in items])
    ).group_by(WorkItemRun.work_item_id)
    rows = (await db.execute(select(WorkItemRun, RoutineRun).outerjoin(
        RoutineRun, RoutineRun.routine_id == WorkItemRun.routine_id).where(WorkItemRun.id.in_(latest_ids)))).all()
    statuses = {}
    for receipt, run in rows:
        state = execution_state(receipt, run)
        statuses[receipt.work_item_id] = dict(execution_state=state, execution_attempt=receipt.attempt,
                                             execution_thread_id=run.thread_id if run else None)
    return [WorkResponse.model_validate(item).model_copy(update=statuses.get(item.id, {})) for item in items]


async def start(db, owner, item_id, payload):
    await begin_write(db)
    item = await owned_item(db, owner, item_id, lock=True)
    previous = await db.scalar(select(WorkItemRun).where(
        WorkItemRun.owner_id == owner, WorkItemRun.work_item_id == item_id,
        WorkItemRun.idempotency_key == payload.idempotency_key))
    if previous:
        return await describe(db, previous)
    if item.archived:
        raise error(409, "Restore the task before starting work")
    if item.revision != payload.revision:
        raise error(409, "The task changed. Reload it before starting work.")
    receipts = (await db.scalars(select(WorkItemRun).where(WorkItemRun.work_item_id == item.id))).all()
    for receipt in receipts:
        status = (await describe(db, receipt)).state
        if status in (*ACTIVE_RUN_STATES, "scheduled", "claimed"):
            raise error(409, "This task already has scheduled or active work. Open or cancel that attempt first.")
    data = dict(agent_id=payload.agent_id, name=f"Task #{item.id}: {item.title}"[:80],
                prompt=payload.prompt, approval_mode=payload.approval_mode, timezone=item.timezone,
                schedule_kind="once" if payload.scheduled_for else "manual",
                schedule_spec={"at": payload.scheduled_for.isoformat()} if payload.scheduled_for else {},
                retry_limit=0)
    try:
        routine = await routines.create_routine(db, owner, data, commit=False)
    except routines.InvalidRoutine as exc:
        raise error(422, str(exc)) from None
    attempt = (await db.scalar(select(func.max(WorkItemRun.attempt)).where(WorkItemRun.work_item_id == item.id)) or 0) + 1
    receipt = WorkItemRun(owner_id=owner, work_item_id=item.id, routine_id=routine.id,
                          idempotency_key=payload.idempotency_key, attempt=attempt)
    db.add(receipt)
    await db.flush()
    work_events.add(db, item, "scheduled" if payload.scheduled_for else "queued", receipt_id=receipt.id)
    if payload.scheduled_for is None:
        # Bypass the public managed-routine guard: all three records commit
        # atomically before the scheduler can see the queued turn.
        run = RoutineRun(routine_id=routine.id, trigger="manual", state="queued",
                         scheduled_for=datetime.now(UTC), prompt=routine.prompt)
        db.add(run)
    await db.commit()
    return await describe(db, receipt)


async def cancel(scheduler, owner, item_id, receipt_id):
    # Serialize against this process's start/send boundary. A standby process
    # refuses an in-flight claim instead of claiming it stopped remote work.
    async with scheduler._lock:
        async with scheduler.session() as db:
            await begin_write(db)
            await owned_item(db, owner, item_id)
            receipt = await db.scalar(select(WorkItemRun).where(
                WorkItemRun.id == receipt_id, WorkItemRun.work_item_id == item_id,
                WorkItemRun.owner_id == owner))
            if receipt is None:
                raise error(404, "Task attempt not found")
            routine = await db.get(Routine, receipt.routine_id, with_for_update=True)
            run = await db.scalar(select(RoutineRun).where(RoutineRun.routine_id == routine.id)
                                  .with_for_update())
            if receipt.cancelled_at:
                return await describe(db, receipt)
            if run and run.state in ("running", "needs_input"):
                if run.thread_id is None or run.turn_id is None:
                    raise error(409, "This attempt is starting. Try Stop again in a moment.")
                # Release SQLite's write lock while the runtime writes the stop.
                thread_id, run_id = run.thread_id, run.id
                await db.rollback()
                try:
                    await scheduler.runtime.stop_turn(thread_id)
                except routines.runtime_module.TurnNotHere:
                    raise error(409, "This attempt runs in another process. Stop it from its conversation there.") from None
                except routines.runtime_module.NoRunningTurn:
                    pass
                await scheduler._follow(run_id)
                await begin_write(db)
                await db.refresh(receipt)
                await db.refresh(routine, with_for_update=True)
                await db.refresh(run, with_for_update=True)
                # A completion racing Stop keeps its actual outcome.
                if run.state not in ACTIVE_RUN_STATES and run.error != "The run was stopped.":
                    return await describe(db, receipt)
            elif run and run.state not in ACTIVE_RUN_STATES:
                return await describe(db, receipt)
            now = datetime.now(UTC)
            routine.paused, routine.next_run_at = True, None
            receipt.cancelled_at = now
            work_events.add(db, await owned_item(db, owner, item_id), "cancelled", receipt_id=receipt.id)
            if run:
                run.state, run.finished_at, run.error = "cancelled", now, "Cancelled by the task owner."
            else:
                # Keep a cancelled future occurrence visible on the calendar
                # and occupy the scheduler's unique occurrence key as well.
                db.add(RoutineRun(routine_id=routine.id, trigger="schedule", state="cancelled",
                    scheduled_for=datetime.fromisoformat(routine.schedule_spec["at"]),
                    prompt=routine.prompt, finished_at=now, error="Cancelled by the task owner."))
            await db.commit()
            return await describe(db, receipt)
