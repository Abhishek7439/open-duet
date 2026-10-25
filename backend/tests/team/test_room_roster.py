"""Roster edits preserve history and fence work before changing membership."""
import asyncio
import pytest
from sqlalchemy import delete, select
from app.db.models import Agent, RoomMember, RoomDispatch, Thread
from app.services import agent_store, session_search, memory_room
from app.team.service import TeamError
from tests.services.thread_harness import sessions, owner, cleanup, wait_until  # noqa: F401
from tests.team.test_collaboration import harness  # noqa: F401
from tests.team.test_rooms_router import api, make_room  # noqa: F401


@pytest.fixture
async def foreign_owner(sessions, owner):
    identifier = owner + '-other'
    yield identifier
    async with sessions() as db:
        await db.execute(delete(Agent).where(Agent.owner_id == identifier))
        await db.commit()


def roster(ids, expected, **extra):
    return {'member_ids': ids, 'expected_member_ids': expected, **extra}


async def test_remove_cancels_queue_and_keeps_history(api, harness, sessions):
    rt, engine, lead, worker = harness
    room = await make_room(api, harness, default_responder_kind='everyone')
    await rt.rooms.post(room['id'], 'Keep this shared history')
    response = await api.patch(f"/rooms/{room['id']}", json=roster([lead.id], [lead.id, worker.id]))
    assert response.status_code == 200, response.text
    assert [m['agent_id'] for m in response.json()['members']] == [lead.id]
    async with sessions() as db:
        removed = await db.scalar(select(RoomMember).where(RoomMember.room_id == room['id'], RoomMember.agent_id == worker.id))
        assert removed.removed_at is not None
        skipped = await db.scalar(select(RoomDispatch).where(RoomDispatch.room_id == room['id'], RoomDispatch.agent_id == worker.id))
        assert (skipped.status, skipped.error_code) == ('skipped', 'member_removed')
    await rt.rooms.work(room['id'])
    await rt.rooms.work(room['id'])
    assert len(engine.contexts) == 1
    history = (await api.get(f"/rooms/{room['id']}/messages")).json()
    assert history[0]['text'] == 'Keep this shared history'
    notice = next(row for row in history if row.get('kind') == 'roster_changed')
    assert notice['removed_agent_ids'] == [worker.id]
    exported = await api.get(f"/rooms/{room['id']}/export", params={'format': 'json'})
    assert any(row.get('kind') == 'roster_changed' for row in exported.json()['messages'])
    assert (await api.patch(f"/rooms/{room['id']}/members/{worker.id}", json={'held': False})).status_code == 409
    with pytest.raises(TeamError, match='not a room member'):
        await rt.rooms.post(room['id'], 'no longer a member', caller_id=worker.id)


async def test_readd_reuses_thread_but_not_missed_messages_or_old_work(api, harness, sessions):
    rt, engine, lead, worker = harness
    room = await make_room(api, harness)
    await rt.rooms.post(room['id'], '@Reviewer original request')
    await rt.rooms.work(room['id'])
    old = await rt.rooms.detail(room['id'])
    work_thread = next(m['member_thread_id'] for m in old['members'] if m['agent_id'] == worker.id)
    await rt.rooms.post(room['id'], '@Reviewer OLD QUEUED REQUEST')
    await rt.rooms.update(room['id'], roster([lead.id], [lead.id, worker.id]))
    await rt.rooms.post(room['id'], 'MISSED DURING ABSENCE')
    await rt.rooms.update(room['id'], roster([lead.id, worker.id], [lead.id]))
    restored = await rt.rooms.detail(room['id'])
    assert next(m['member_thread_id'] for m in restored['members'] if m['agent_id'] == worker.id) == work_thread
    await rt.rooms.work(room['id'])
    assert len(engine.contexts) == 1
    await rt.rooms.post(room['id'], '@Reviewer NEW REQUEST')
    await rt.rooms.work(room['id'])
    prompt = engine.contexts[-1].prompt
    assert 'NEW REQUEST' in prompt
    assert 'MISSED DURING ABSENCE' not in prompt and 'OLD QUEUED REQUEST' not in prompt
    async with sessions() as db:
        assert await db.get(Thread, work_thread) is not None
    # Deletion cleans up both active and retired member work threads.
    await rt.rooms.update(room['id'], roster([lead.id], [lead.id, worker.id]))
    await rt.rooms.delete(room['id'])
    async with sessions() as db:
        assert await db.get(Thread, work_thread) is None


@pytest.mark.parametrize('ids,expected,code', [
    ([], [0, 1], 422), (list(range(13)), [0, 1], 422),
    ([0, 0], [0, 1], 409), (None, [0, 1], 409),
    ([0], None, 409), ([0], [0], 409),
])
async def test_invalid_rosters_are_atomic(api, harness, ids, expected, code):
    rt, engine, lead, worker = harness
    room = await make_room(api, harness)
    mapping = {0: lead.id, 1: worker.id}
    translate = lambda values: [mapping.get(i, 10000 + i) for i in values] if values is not None else None
    response = await api.patch(f"/rooms/{room['id']}", json=roster(translate(ids), translate(expected), name='Must roll back'))
    assert response.status_code == code, response.text
    detail = await rt.rooms.detail(room['id'])
    assert detail['name'] == 'Desk' and len(detail['members']) == 2


async def test_remove_default_requires_explicit_new_responder(api, harness):
    rt, engine, lead, worker = harness
    room = await make_room(api, harness, default_responder_kind='member', default_responder_agent_id=worker.id)
    body = roster([lead.id], [lead.id, worker.id])
    assert (await api.patch(f"/rooms/{room['id']}", json=body)).status_code == 409
    body['default_responder_agent_id'] = lead.id
    assert (await api.patch(f"/rooms/{room['id']}", json=body)).status_code == 200


async def test_owner_team_and_archived_members_validated(api, harness, sessions, owner, foreign_owner):
    rt, engine, lead, worker = harness
    room = await make_room(api, harness)
    async with sessions() as db:
        other = await agent_store.create_agent(db, owner_id=foreign_owner, name='Other')
        archived = await agent_store.create_agent(db, owner_id=owner, name='Archived')
        from app.team.service import now
        archived.archived_at = now()
        foreign_team = await agent_store.create_agent(db, owner_id=owner, name='Wrong team')
        foreign_team.team_id = 987654
        standalone = await agent_store.create_agent(db, owner_id=owner, name='Standalone')
        await db.commit()
    for invalid in [other.id, archived.id, foreign_team.id, 999999]:
        response = await api.patch(f"/rooms/{room['id']}", json=roster([lead.id, worker.id, invalid], [lead.id, worker.id]))
        assert response.status_code in (404, 409), response.text
    ok = await api.patch(f"/rooms/{room['id']}", json=roster([lead.id, worker.id, standalone.id], [lead.id, worker.id]))
    assert ok.status_code == 200
    async with sessions() as db:
        from app.db.models import Room
        stored = await db.get(Room, room['id'])
        stored.owner_id = 'another-room-owner'
        await db.commit()
    try:
        assert (await api.patch(f"/rooms/{room['id']}", json=roster([lead.id], [lead.id, worker.id, standalone.id]))).status_code == 404
    finally:
        async with sessions() as db:
            stored = await db.get(Room, room['id'])
            stored.owner_id = owner
            await db.commit()


async def test_concurrent_edit_refuses_stale_roster(harness):
    rt, engine, lead, worker = harness
    room = await rt.rooms.create({'name': 'Concurrent', 'member_ids': [lead.id, worker.id], 'default_responder_kind': 'mentions'})
    results = await asyncio.gather(
        rt.rooms.update(room['id'], roster([lead.id], [lead.id, worker.id], bulletin='first')),
        rt.rooms.update(room['id'], roster([worker.id], [lead.id, worker.id], bulletin='second')),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    refused = next(r for r in results if isinstance(r, TeamError))
    assert refused.code == 'roster_conflict'
    saved = await rt.rooms.detail(room['id'])
    assert len(saved['members']) == 1
    assert saved['bulletin'] == ('first' if saved['members'][0]['agent_id'] == lead.id else 'second')
    assert sum(row.get('kind') == 'roster_changed' for row in await rt.rooms.messages(room['id'])) == 1


async def test_stop_during_startup_keeps_busy_fence_until_worker_settles(harness, sessions, monkeypatch):
    rt, engine, lead, worker = harness
    room = await rt.rooms.create({'name': 'Startup', 'member_ids': [lead.id, worker.id], 'default_responder_kind': 'mentions'})
    await rt.rooms.post(room['id'], '@Reviewer work')
    entered, release = asyncio.Event(), asyncio.Event()
    original = rt.create_thread
    async def paused_create(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(rt, 'create_thread', paused_create)
    task = asyncio.create_task(rt.rooms.work(room['id']))
    rt.rooms.tasks[room['id']] = task
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await rt.rooms.stop(room['id'], worker.id) == 1
        with pytest.raises(TeamError) as exc:
            await rt.rooms.update(room['id'], roster([lead.id], [lead.id, worker.id]))
        assert exc.value.code == 'member_busy'
        release.set()
        await asyncio.wait_for(task, 5)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert not engine.contexts
    async with sessions() as db:
        row = await db.scalar(select(RoomDispatch).where(RoomDispatch.room_id == room['id']))
        assert row.status == 'stopped'
    await rt.rooms.update(room['id'], roster([lead.id], [lead.id, worker.id]))


async def test_running_turn_must_settle_and_late_results_are_discarded(harness, sessions):
    rt, engine, lead, worker = harness
    room = await rt.rooms.create({'name': 'Running', 'member_ids': [lead.id, worker.id], 'default_responder_kind': 'mentions'})
    await rt.rooms.post(room['id'], '@Reviewer BLOCK')
    await rt.rooms.tick()
    async def running():
        async with sessions() as db:
            return await db.scalar(select(RoomDispatch).where(RoomDispatch.room_id == room['id'], RoomDispatch.turn_id.is_not(None)))
    await wait_until(running)
    with pytest.raises(TeamError) as exc:
        await rt.rooms.update(room['id'], roster([lead.id], [lead.id, worker.id]))
    assert exc.value.code == 'member_busy'
    await rt.rooms.stop(room['id'], worker.id)
    await wait_until(lambda: rt.rooms.tasks[room['id']].done())
    await rt.rooms.update(room['id'], roster([lead.id], [lead.id, worker.id]))
    assert not any(message.get('author', {}).get('kind') == 'agent' for message in await rt.rooms.messages(room['id']))


async def test_old_failed_replies_cannot_retry_after_readd(api, harness, sessions):
    rt, engine, lead, worker = harness
    room = await make_room(api, harness)
    await rt.rooms.post(room['id'], '@Reviewer earlier request')
    async with sessions() as db:
        row = await db.scalar(select(RoomDispatch).where(RoomDispatch.room_id == room['id']))
        row.status = 'failed'
        await db.commit()
        dispatch_id = row.id
    await rt.rooms.update(room['id'], roster([lead.id], [lead.id, worker.id]))
    await rt.rooms.update(room['id'], roster([lead.id, worker.id], [lead.id]))
    response = await api.post(f"/rooms/{room['id']}/dispatches/{dispatch_id}/retry")
    assert response.status_code == 409
    assert not engine.contexts


async def test_removed_member_loses_room_recall_and_memory_capture(harness, sessions, monkeypatch):
    rt, engine, lead, worker = harness
    room = await rt.rooms.create({'name': 'Private', 'member_ids': [lead.id, worker.id], 'default_responder_kind': 'mentions'})
    await rt.rooms.post(room['id'], 'rostersecret')
    await rt.rooms.update(room['id'], roster([lead.id], [lead.id, worker.id]))
    monkeypatch.setattr(memory_room.memory_settings, 'learn_from_rooms', lambda: True)
    async with sessions() as db:
        assert await memory_room.live_room(db, rt.owner_id, room['thread_id'], worker.id) is None
        result = await session_search.search(db, owner_id=rt.owner_id, agent_id=worker.id, current_thread_id=None, query='rostersecret', scope='conversations')
        assert not result.conversations
        kept = await session_search.search(db, owner_id=rt.owner_id, agent_id=lead.id, current_thread_id=None, query='rostersecret', scope='conversations')
        assert kept.conversations


@pytest.mark.parametrize('preceding_state', ['stop_requested', 'skipped'])
async def test_finish_waits_for_room_before_locking_dispatch(harness, sessions, monkeypatch, preceding_state):
    """A real PG waiter must not stop the room holder updating its dispatch.

    The old dispatch->room order deadlocked against Stop's room->dispatch
    order. A server-observed wait barrier makes that interleaving deterministic.
    """
    from uuid import uuid4
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.core.config import settings
    from app.db.models import Room
    if settings.DATABASE_KIND != 'postgres':
        pytest.skip('asserts PostgreSQL row-lock order with independent sessions')
    rt, engine, lead, worker = harness
    room = await rt.rooms.create({'name': 'Lock order', 'member_ids': [lead.id], 'default_responder_kind': 'member'})
    await rt.rooms.post(room['id'], 'Finish after concurrent room change')
    async with sessions() as db:
        dispatch = await db.scalar(select(RoomDispatch).where(RoomDispatch.room_id == room['id']))
        dispatch.status = 'running'
        await db.commit()
        dispatch_id = dispatch.id
    application = 'room_finish_' + uuid4().hex
    finish_engine = create_async_engine(settings.ASYNC_DATABASE_URL, connect_args={
        'server_settings': {'application_name': application},
    })
    finish_sessions = async_sessionmaker(finish_engine, expire_on_commit=False)
    task = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(rt, '_session_factory', finish_sessions)
            async with sessions() as holder:
                await holder.get(Room, room['id'], with_for_update=True)
                task = asyncio.create_task(rt.rooms.finish(dispatch_id, None))
                try:
                    async with asyncio.timeout(5):
                        while True:
                            async with sessions() as monitor:
                                waiting = await monitor.scalar(text(
                                    "SELECT count(*) FROM pg_stat_activity WHERE application_name = :app "
                                    "AND wait_event_type = 'Lock' AND query LIKE '%FOR UPDATE%'"
                                ), {'app': application})
                            if waiting == 1:
                                break
                            assert not task.done()
                            await asyncio.sleep(.01)
                    # If finish held the dispatch while waiting on our room,
                    # this write would time out (or PostgreSQL would deadlock).
                    await holder.execute(text("SET LOCAL lock_timeout = '1s'"))
                    current = await holder.get(RoomDispatch, dispatch_id, with_for_update=True)
                    if preceding_state == 'stop_requested':
                        current.reason = preceding_state
                    else:
                        current.status = preceding_state
                    await holder.commit()
                finally:
                    await holder.rollback()
            await asyncio.wait_for(task, 5)
        async with sessions() as db:
            current = await db.get(RoomDispatch, dispatch_id)
            assert current.status == ('stopped' if preceding_state == 'stop_requested' else 'skipped')
    finally:
        if task and not task.done():
            task.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await finish_engine.dispose()
