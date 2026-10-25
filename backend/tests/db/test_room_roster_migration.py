"""Adding roster history leaves existing rooms and members active."""
import sqlite3
from tests.db.sqlite_helpers import run_alembic


def test_existing_members_survive_roster_upgrade_and_downgrade(tmp_path):
    database = tmp_path / 'rooms.db'
    old = run_alembic(database, 'upgrade', 'b2e7c1d9a504')
    assert old.returncode == 0, old.stderr
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO room (id, owner_id, name, thread_id) VALUES (1, 'owner', 'Retain room', 700)")
        db.execute("INSERT INTO room_member (room_id, agent_id, member_thread_id, last_seen_seq) VALUES (1, 22, 701, 9)")
    upgraded = run_alembic(database, 'upgrade', 'head')
    assert upgraded.returncode == 0, upgraded.stderr
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT agent_id, member_thread_id, last_seen_seq, removed_at FROM room_member').fetchone() == (22, 701, 9, None)
    downgraded = run_alembic(database, 'downgrade', 'b2e7c1d9a504')
    assert downgraded.returncode == 0, downgraded.stderr
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT agent_id, member_thread_id, last_seen_seq FROM room_member').fetchone() == (22, 701, 9)
