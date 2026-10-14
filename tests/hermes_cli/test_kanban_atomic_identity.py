"""Native task identity races and migration refusal on disposable boards."""
import contextlib
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / "board.db"
    kb.init_db(path)
    return path


def create(conn, key="identity"):
    return kb.create_task(conn, title="selected", idempotency_key=key,
                          workspace_kind="scratch", project_id="",
                          initial_status="blocked")


def test_stale_legacy_insert_cannot_duplicate_identity(board):
    with contextlib.closing(kbc.connect(board)) as conn:
        assert conn.execute("SELECT id FROM tasks WHERE idempotency_key = ?",
                            ("identity",)).fetchall() == []
        task_id = create(conn)
        with pytest.raises(sqlite3.IntegrityError):
            with kb.write_txn(conn):
                conn.execute(
                    "INSERT INTO tasks(id,title,status,created_at,idempotency_key) "
                    "VALUES ('legacy','selected','blocked',0,'identity')"
                )
        rows = conn.execute("SELECT id FROM tasks WHERE idempotency_key = ?",
                            ("identity",)).fetchall()
        assert [row["id"] for row in rows] == [task_id]


def test_lookup_occurs_under_write_transaction(board):
    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection
            self.lookups = 0

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, parameters=()):
            if sql.startswith("SELECT id FROM tasks WHERE idempotency_key"):
                assert self.connection.in_transaction
                self.lookups += 1
            return self.connection.execute(sql, parameters)

    with contextlib.closing(kbc.connect(board)) as conn:
        observed = ObservedConnection(conn)
        first = create(observed)
        assert create(observed) == first
        assert observed.lookups == 2
        assert not conn.in_transaction
        assert conn.execute("SELECT count(*) FROM task_events WHERE kind='created'").fetchone()[0] == 1


def test_duplicate_migration_preserves_existing_rows(board):
    with contextlib.closing(kbc.connect(board)) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_tasks_active_idempotency")
        conn.execute(
            "INSERT INTO tasks(id,title,status,created_at,idempotency_key) VALUES "
            "('a','selected','blocked',0,'identity'),"
            "('b','selected','blocked',0,'identity')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        kb.init_db(board)
    with contextlib.closing(sqlite3.connect(board)) as conn:
        assert conn.execute("SELECT id FROM tasks ORDER BY id").fetchall() == [("a",), ("b",)]


def test_archived_and_empty_identity_keep_native_reuse_semantics(board):
    with contextlib.closing(kbc.connect(board)) as conn:
        old = create(conn)
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (old,))
        fresh = create(conn)
        assert fresh != old
        assert create(conn, "") != create(conn, "")
        assert create(conn, None) != create(conn, None)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (old,))


def test_nested_create_is_rolled_back_with_outer_transaction(board):
    with contextlib.closing(kbc.connect(board)) as conn:
        with pytest.raises(RuntimeError, match="outer abort"):
            with kb.write_txn(conn):
                create(conn)
                raise RuntimeError("outer abort")
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM task_events").fetchone()[0] == 0


def test_ambiguous_identity_refuses_without_mutation(board):
    with contextlib.closing(kbc.connect(board)) as conn:
        conn.execute("DROP INDEX idx_tasks_active_idempotency")
        conn.execute(
            "INSERT INTO tasks(id,title,status,created_at,idempotency_key) VALUES "
            "('a','selected','blocked',0,'identity'),"
            "('b','selected','blocked',0,'identity')"
        )
        before = conn.total_changes
        with pytest.raises(ValueError, match="ambiguous"):
            create(conn)
        assert conn.total_changes == before
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2


@pytest.mark.platforms("posix")
def test_independent_process_creators_return_same_identity(board):
    import select
    import subprocess
    import sys

    code = """
import contextlib, sys
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from pathlib import Path
with contextlib.closing(kbc.connect(Path(sys.argv[1]))) as conn:
    print('ready', flush=True)
    assert sys.stdin.readline().strip() == 'create'
    result = kb.create_task(conn, title='selected', idempotency_key='identity',
                            workspace_kind='scratch', project_id='', initial_status='blocked')
    print(result, flush=True)
"""
    children = []
    try:
        for _ in range(2):
            children.append(subprocess.Popen(
                [sys.executable, "-c", code, str(board)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        for child in children:
            assert select.select([child.stdout], [], [], 10)[0], "child did not become ready"
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            child.stdin.write("create\n")
            child.stdin.flush()
        results = []
        for child in children:
            output, errors = child.communicate(timeout=10)
            assert child.returncode == 0, errors
            results.append(output.strip())
        assert results[0] == results[1]
        assert results[0].startswith("t_")
        with contextlib.closing(kbc.connect(board)) as conn:
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM task_events WHERE kind='created'").fetchone()[0] == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate()
