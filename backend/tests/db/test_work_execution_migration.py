import sqlite3

from tests.db.sqlite_helpers import run_alembic


def test_existing_cards_survive_upgrade_and_execution_downgrade(tmp_path):
    database = tmp_path / "planner.db"
    baseline = run_alembic(database, "upgrade", "9e6d3b1a7c52")
    assert baseline.returncode == 0, baseline.stderr
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO work_board (id, owner_id, key, name) VALUES (1, 'owner', 'default', 'My tasks')")
        db.execute("INSERT INTO work_column (id, board_id, key, name, position) VALUES (1, 1, 'ready', 'Ready', 0)")
        db.execute("INSERT INTO work_item (owner_id, board_id, column_id, title, position) VALUES ('owner', 1, 1, 'Keep this task', 0)")
    upgraded = run_alembic(database, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT title FROM work_item").fetchone() == ("Keep this task",)
        assert db.execute("SELECT count(*) FROM work_item_run").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM work_item_event").fetchone() == (0,)
    downgraded = run_alembic(database, "downgrade", "9e6d3b1a7c52")
    assert downgraded.returncode == 0, downgraded.stderr
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT title FROM work_item").fetchone() == ("Keep this task",)
