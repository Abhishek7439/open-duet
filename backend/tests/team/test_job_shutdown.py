"""A scheduler pass finishes its write before shutdown releases its resources."""
import asyncio
from types import SimpleNamespace

from app.db.transactions import begin_write
from app.team.jobs import Jobs
from tests.services.thread_harness import sessions  # noqa: F401


async def test_close_drains_current_transaction_and_refuses_new_passes(sessions):
    jobs = Jobs(SimpleNamespace(_closing=False))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def tick():
        calls.append(True)
        async with sessions() as db:
            await begin_write(db)
            entered.set()
            await release.wait()
            await db.commit()

    jobs.tick = tick
    jobs.kick()
    await entered.wait()
    closing = asyncio.create_task(jobs.close())
    try:
        await asyncio.sleep(0)
        jobs.kick()
        assert not closing.done()
        assert not jobs._kick_task.done()
    finally:
        release.set()
        await closing
    assert len(calls) == 1
    async with sessions() as db:
        await begin_write(db)
        await db.rollback()
    jobs.kick()
    assert len(calls) == 1
    jobs.reopen()
    jobs.kick()
    await jobs._kick_task
    assert len(calls) == 2


async def test_close_does_not_run_a_requested_followup_pass():
    jobs = Jobs(SimpleNamespace(_closing=False))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def tick():
        calls.append(True)
        entered.set()
        await release.wait()

    jobs.tick = tick
    jobs.kick()
    await entered.wait()
    jobs.kick()
    closing = asyncio.create_task(jobs.close())
    await asyncio.sleep(0)
    release.set()
    await closing
    assert len(calls) == 1
