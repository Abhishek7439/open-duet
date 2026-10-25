"""Routine receipt Undo: settings only, never replay executions or restore grants."""
from sqlalchemy import func, select

from app.db.models import Agent, Routine
from app.db.models.routine import RoutineRun
from app.services import routine_service
from app.team import service, setup

FIELDS = ('agent_id', 'name', 'prompt', 'schedule_kind', 'schedule_spec', 'timezone',
          'run_limit_minutes', 'approval_mode', 'approval_timeout_minutes',
          'catch_up_minutes', 'retry_limit', 'retry_delay_minutes')
UNDO_NOTE = ('Undo restores saved settings paused. Resume separately. It never replays runs, '
             'restores deleted history, or restores a webhook grant.')


def fingerprint(routine):
    return setup.digest({column.name: getattr(routine, column.name) for column in Routine.__table__.columns})


async def runs_marker(db, routine_id):
    count, latest = (await db.execute(select(func.count(), func.max(RoutineRun.id)).where(
        RoutineRun.routine_id == routine_id))).one()
    return [count, latest]


async def capture(db, routine, action):
    before = {field: getattr(routine, field) for field in FIELDS}
    safe = setup.redacted(before)
    return {'routine_id': routine.id, 'agent_id': routine.agent_id, 'action': action,
            'before': safe if safe == before else None,
            'had_active_run': await routine_service._has_active_run(db, routine.id),
            'runs': await runs_marker(db, routine.id)}


async def status(db, receipt, *, lock=False):
    if receipt.state != 'applied':
        return {'available': False, 'reason': 'This change is no longer undoable.'}
    entry = receipt.undo or {}
    if not entry or entry.get('before') is None:
        return {'available': False, 'reason': 'Undo is unavailable for settings containing secret-looking text.'}
    if entry.get('had_active_run'):
        return {'available': False, 'reason': 'A run was already queued or active when this change was made.'}
    team = await service.team_for(db, receipt.owner_id)
    agent = await db.get(Agent, entry['agent_id'], with_for_update=lock, populate_existing=True)
    if (team is None or team.id != receipt.team_id or agent is None or
            agent.owner_id != receipt.owner_id or agent.team_id != team.id or agent.archived_at is not None):
        return {'available': False, 'reason': 'The agent or team changed.'}
    routine = await db.get(Routine, entry['routine_id'], with_for_update=lock, populate_existing=True)
    if entry['action'] == 'delete':
        if routine is not None or entry['runs'][0]:
            return {'available': False, 'reason': 'Deleted run history cannot be restored.'}
    else:
        if (routine is None or routine.owner_id != receipt.owner_id or
                fingerprint(routine) != entry['hash']):
            return {'available': False, 'reason': 'The routine changed after this action.'}
        if await runs_marker(db, routine.id) != entry['runs'] or await routine_service._has_active_run(db, routine.id):
            return {'available': False, 'reason': 'The routine has queued or recorded a run since this action.'}
    return {'available': True, 'reason': None}


async def undo(db, owner, receipt, team):
    check = await status(db, receipt, lock=True)
    if not check['available']:
        raise service.TeamError('changed_since', check['reason'])
    entry = receipt.undo
    routine = await db.get(Routine, entry['routine_id'], with_for_update=True)
    if entry['action'] == 'create':
        await routine_service.delete_routine(db, owner, routine.id, commit=False)
        result = {'routine_id': entry['routine_id'], 'removed': True}
    elif entry['action'] == 'delete':
        # A new identity intentionally prevents old delivery keys/links from
        # becoming valid again. History-bearing deletions cannot be undone.
        routine = await routine_service.create_routine(db, owner, {**entry['before'], 'paused': True, 'approval_mode': 'ask'}, commit=False)
        result = {'routine_id': routine.id, 'paused': True}
    else:
        await routine_service.update_routine(db, owner, routine.id, entry['before'], commit=False)
        routine = await routine_service.set_paused(db, owner, routine.id, True, commit=False)
        routine.updated_at = service.now()
        result = {'routine_id': routine.id, 'paused': True}
    receipt.state, receipt.decided_at = 'undone', service.now()
    receipt.result = {**receipt.result, 'undo_result': result}
    service.audit(db, team, 'routine_change_undone', agent_id=entry['agent_id'],
                  data={'request_id': receipt.request_id, 'routine_id': result['routine_id']})
    await db.commit()
    return {'request_id': receipt.request_id, 'state': 'undone', **result, 'note': UNDO_NOTE}
