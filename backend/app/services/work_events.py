"""Small transaction-local planner event helpers shared with the executor."""
from sqlalchemy import select

from app.db.models.work_item import WorkItemEvent, WorkItemRun


def add(db, item, kind, *, receipt_id=None, actor="owner", detail=None, key=None):
    event = WorkItemEvent(owner_id=item.owner_id, work_item_id=item.id, receipt_id=receipt_id,
                          kind=kind, actor=actor, detail=detail)
    if key is not None:
        event.key = key
    db.add(event)
    return event


async def execution(db, run, kind):
    receipt = await db.scalar(select(WorkItemRun).where(WorkItemRun.routine_id == run.routine_id))
    if receipt:
        db.add(WorkItemEvent(owner_id=receipt.owner_id, work_item_id=receipt.work_item_id,
                            receipt_id=receipt.id, kind=kind, actor="executor", detail=run.error))
