"""Deadline notifications share the routine tick, and never enqueue execution."""
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import exists, or_, select

from app.db.models import WorkColumn, WorkItem, WorkItemEvent
from app.db.transactions import begin_write
from app.services import notification_feed, work_events


async def remind_due(session, owner, now):
    texts = []
    async with session() as db:
        await begin_write(db)
        candidates = (await db.scalars(select(WorkItem).join(WorkColumn).where(
            WorkItem.owner_id == owner, WorkItem.archived.is_(False), WorkColumn.key != "done",
            or_(WorkItem.due_at <= now, WorkItem.due_date <= (now + timedelta(days=1)).date()))
            .with_for_update(of=WorkItem, skip_locked=True))).all()
        for item in candidates:
            if item.due_at:
                key = f"deadline:{item.due_at.isoformat()}"
                detail = f"Due {item.due_at.astimezone(ZoneInfo(item.timezone)).isoformat()} ({item.timezone})."
            elif item.due_date and item.due_date <= now.astimezone(ZoneInfo(item.timezone)).date():
                key = f"deadline:{item.due_date.isoformat()}"
                detail = f"Due {item.due_date.isoformat()} ({item.timezone})."
            else:
                continue
            sent = await db.scalar(select(exists().where(
                WorkItemEvent.work_item_id == item.id, WorkItemEvent.key == key)))
            if sent:
                continue
            work_events.add(db, item, "deadline_reminder", actor="scheduler", key=key, detail=detail)
            notification = notification_feed.add(db, owner_id=owner, kind="task_due",
                title=f"Task due: {item.title}", body=f"{detail} Open Tasks & calendar to review it. This reminder does not start agent work.",
                thread_id=item.thread_id)
            texts.append(notification_feed.telegram_text(notification))
        await db.commit()
    return texts
