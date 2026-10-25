"""Person Undo is revision-bound, serialised, and never replays routine runs."""
import asyncio
import pytest
from sqlalchemy import select
from app.db.models import Routine, TeamSetupRequest
from app.db.models.routine import RoutineRun
from app.services import routine_service
from app.team import management, setup, service
from tests.team.test_collaboration import harness  # noqa: F401
from tests.team.test_management import run_tools
from tests.services.thread_harness import sessions, owner, cleanup  # noqa: F401


async def change(harness, action='create'):
    async def operation(rt, run, worker):
        made = await management.call(rt, run, 'create_routine', {
            'agent_id': worker.id, 'name': 'Morning', 'prompt': 'Read the provided files.',
            'schedule_kind': 'daily', 'schedule_spec': {'time': '09:00'}, 'timezone': 'Asia/Kolkata'})
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
