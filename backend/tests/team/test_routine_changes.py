"""Person Undo is revision-bound, serialised, and never replays routine runs."""
import asyncio
import pytest
from sqlalchemy import select
from app.db.models import Routine, TeamSetupRequest
from app.db.models.routine import RoutineRun
from app.services import routine_service
from app.team import management, routine_changes, setup, service
from tests.team.test_collaboration import harness  # noqa: F401
from tests.team.test_management import run_tools
from tests.services.thread_harness import sessions, owner, cleanup  # noqa: F401


async def change(harness, action='create'):
    async def operation(rt, run, worker):
        made = await management.call(rt, run, 'create_routine', {
            'agent_id': worker.id, 'name': 'Morning', 'prompt': 'Read the provided files.',
            'schedule_kind': 'cron', 'schedule_spec': {'expression': '0 9 * * *'}, 'timezone': 'Asia/Kolkata'})
        if action == 'create':
            return made
        routine = made['routine']
        args = {'agent_id': worker.id, 'routine_id': routine['id'], 'action': action,
                'expected_revision': routine['revision']}
        if action == 'update':
            args['changes'] = {'name': 'Evening'}
        return await management.call(rt, run, 'manage_routine', args)
    return await run_tools(harness, operation)


@pytest.mark.parametrize('action', ['create', 'update', 'pause', 'resume', 'delete'])
async def test_undo_settings_is_paused_and_replay_safe(harness, sessions, owner, action):
    result = await change(harness, action)
    async with sessions() as db:
        receipt = await setup.existing(db, owner, result['request_id'])
        assert receipt.kind == 'routine'
        assert (await setup.undo_status(db, receipt))['available']
        undone = await setup.undo(db, owner, receipt.request_id)
        routine = await db.get(Routine, undone['routine_id'])
        if action == 'create':
            assert routine is None
        else:
            assert routine.paused and routine.next_run_at is None
            assert routine.name == 'Morning' and routine.approval_mode == 'ask'
            assert routine.webhook_secret_hash is None
        if action == 'delete':
            assert undone['routine_id'] != result['routine']['id']
        with pytest.raises(service.TeamError) as exc:
            await setup.undo(db, owner, result['request_id'])
        assert exc.value.code == 'already_undone'


async def test_undo_refuses_later_settings_and_does_not_overwrite(harness, sessions, owner):
    result = await change(harness)
    async with sessions() as db:
        await routine_service.update_routine(db, owner, result['routine']['id'], {'name': 'Later edit'})
        with pytest.raises(service.TeamError) as exc:
            await setup.undo(db, owner, result['request_id'])
        assert exc.value.code == 'changed_since'
        await db.rollback()
        assert (await db.get(Routine, result['routine']['id'])).name == 'Later edit'


@pytest.mark.parametrize('run_state', ['queued', 'running', 'completed', 'failed', 'interrupted', 'missed'])
async def test_any_later_run_makes_undo_unavailable(harness, sessions, owner, run_state):
    result = await change(harness, 'resume')
    async with sessions() as db:
        db.add(RoutineRun(routine_id=result['routine']['id'], state=run_state, trigger='manual'))
        await db.commit()
        receipt = await setup.existing(db, owner, result['request_id'])
        assert not (await setup.undo_status(db, receipt))['available']
        with pytest.raises(service.TeamError):
            await setup.undo(db, owner, receipt.request_id)
        await db.rollback()
        assert len(list((await db.execute(select(RoutineRun).where(RoutineRun.routine_id == result['routine']['id']))).scalars())) == 1


async def test_only_one_concurrent_undo_and_owner_scope(harness, sessions, owner):
    result = await change(harness)
    async with sessions() as db:
        with pytest.raises(service.TeamError) as exc:
            await setup.undo(db, 'another-owner', result['request_id'])
        assert exc.value.code == 'request_not_found'
    async def undo():
        async with sessions() as db:
            try:
                return (await setup.undo(db, owner, result['request_id']))['state']
            except service.TeamError as exc:
                return exc.code
    assert sorted(await asyncio.gather(undo(), undo())) == ['already_undone', 'undone']


async def test_deleted_history_is_not_advertised_as_restorable(harness, sessions, owner):
    async def operation(rt, run, worker):
        made = await management.call(rt, run, 'create_routine', {'agent_id': worker.id, 'name': 'History', 'prompt': 'Read files'})
        async with sessions() as db:
            db.add(RoutineRun(routine_id=made['routine']['id'], state='completed', trigger='manual'))
            await db.commit()
        return await management.call(rt, run, 'manage_routine', {'agent_id': worker.id, 'routine_id': made['routine']['id'],
             'action': 'delete', 'expected_revision': made['routine']['revision']})
    result = await run_tools(harness, operation)
    async with sessions() as db:
        receipt = await setup.existing(db, owner, result['request_id'])
        status = await setup.undo_status(db, receipt)
        assert not status['available'] and 'history' in status['reason']
        assert await db.get(Routine, result['routine']['id']) is None


async def test_change_during_an_existing_run_stays_non_undoable_after_it_finishes(harness, sessions, owner):
    async def operation(rt, run, worker):
        made = await management.call(rt, run, 'create_routine', {'agent_id': worker.id, 'name': 'Busy', 'prompt': 'Read files'})
        async with sessions() as db:
            db.add(RoutineRun(routine_id=made['routine']['id'], state='running', trigger='manual'))
            await db.commit()
        return await management.call(rt, run, 'manage_routine', {'agent_id': worker.id,
            'routine_id': made['routine']['id'], 'action': 'pause', 'expected_revision': made['routine']['revision']})
    result = await run_tools(harness, operation)
    async with sessions() as db:
        run = await db.scalar(select(RoutineRun).where(RoutineRun.routine_id == result['routine']['id']))
        run.state = 'completed'
        await db.commit()
        receipt = await setup.existing(db, owner, result['request_id'])
        check = await setup.undo_status(db, receipt)
        assert not check['available'] and 'already queued or active' in check['reason']
        with pytest.raises(service.TeamError):
            await setup.undo(db, owner, result['request_id'])


async def test_manual_admission_refreshes_a_previously_read_definition(harness, sessions, owner):
    result = await change(harness)
    routine_id = result['routine']['id']
    async with sessions() as reader, sessions() as writer:
        cached = await reader.get(Routine, routine_id)
        # End the reader's snapshot while retaining its identity-map entry.
        await reader.commit()
        await routine_service.update_routine(writer, owner, routine_id, {'name': 'Current name', 'prompt': 'Current instructions'})
        run = await routine_service.run_now(reader, owner, routine_id)
        assert cached.name == 'Current name'
        assert 'Current name' in run.prompt and 'Current instructions' in run.prompt


async def test_manual_admission_refuses_a_previously_read_deleted_definition(harness, sessions, owner):
    result = await change(harness)
    routine_id = result['routine']['id']
    async with sessions() as reader, sessions() as writer:
        cached = await reader.get(Routine, routine_id)
        await reader.commit()
        await routine_service.delete_routine(writer, owner, routine_id)
        with pytest.raises(routine_service.RoutineNotFound):
            await routine_service.run_now(reader, owner, cached.id)


async def test_two_manual_admissions_queue_only_one_run(harness, sessions, owner, monkeypatch):
    result = await change(harness)
    routine_id = result['routine']['id']
    checked, release = asyncio.Event(), asyncio.Event()
    original = routine_service._has_active_run
    first = True

    async def active(db, target, trigger=None):
        nonlocal first
        value = await original(db, target, trigger)
        if target == routine_id and first:
            first = False
            checked.set()
            await release.wait()
        return value

    monkeypatch.setattr(routine_service, '_has_active_run', active)

    async def admit():
        async with sessions() as db:
            try:
                return (await routine_service.run_now(db, owner, routine_id)).state
            except routine_service.RoutineBusy:
                return 'busy'

    first_task = asyncio.create_task(admit())
    await asyncio.wait_for(checked.wait(), 5)
    second_task = asyncio.create_task(admit())
    try:
        # The second admission must wait for the first admission's result.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second_task), .1)
    finally:
        release.set()
        outcomes = await asyncio.gather(first_task, second_task)
    assert sorted(outcomes) == ['busy', 'queued']
    async with sessions() as db:
        runs = list((await db.scalars(select(RoutineRun).where(RoutineRun.routine_id == routine_id))).all())
        assert len(runs) == 1


async def test_manual_admission_after_undo_uses_restored_settings(harness, sessions, owner, monkeypatch):
    result = await change(harness, 'update')
    routine_id = result['routine']['id']
    checked, release, admission_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_status, original_get = routine_changes.status, routine_service.get_routine

    async def status(db, receipt, *, lock=False):
        value = await original_status(db, receipt, lock=lock)
        if lock:
            assert value['available']
            checked.set()
            await release.wait()
        return value

    async def get(db, owner_id, target, **kwargs):
        if asyncio.current_task().get_name() == 'admission-after-undo':
            admission_started.set()
        return await original_get(db, owner_id, target, **kwargs)

    monkeypatch.setattr(routine_changes, 'status', status)
    monkeypatch.setattr(routine_service, 'get_routine', get)

    async def undo():
        async with sessions() as db:
            return await setup.undo(db, owner, result['request_id'])

    async def admit():
        async with sessions() as db:
            return await routine_service.run_now(db, owner, routine_id)

    undo_task = asyncio.create_task(undo())
    await asyncio.wait_for(checked.wait(), 5)
    admission_task = asyncio.create_task(admit(), name='admission-after-undo')
    try:
        # SQLite waits at BEGIN IMMEDIATE; PostgreSQL waits at the row lock.
        if routine_service.settings.DATABASE_KIND != 'sqlite':
            await asyncio.wait_for(admission_started.wait(), 5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(admission_task), .1)
    finally:
        release.set()
        undone, run = await asyncio.gather(undo_task, admission_task)
    assert undone['state'] == 'undone'
    assert 'Morning' in run.prompt and 'Evening' not in run.prompt
    async with sessions() as db:
        routine = await db.get(Routine, routine_id)
        assert routine.name == 'Morning' and routine.paused
