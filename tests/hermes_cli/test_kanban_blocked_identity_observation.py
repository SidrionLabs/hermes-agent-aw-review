"""Current identity observations use real read-only WAL transactions, never init."""
import argparse
import contextlib
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from hermes_cli import kanban as kc, kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_blocked_identity import readonly_connection
from hermes_cli.kanban_db_blocked_identity import (
    observe_blocked_identity as observe, reconcile_blocked_identity as reconcile,
)
from tests.hermes_cli.test_kanban_blocked_identity import request_for


@pytest.fixture
def environment(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path.resolve())
    return home


def invoke(path):
    parser = argparse.ArgumentParser()
    kc.build_parser(parser.add_subparsers())
    return kc.kanban_command(parser.parse_args([
        "kanban", "inspect-blocked", "--request-file", str(path),
    ]))


def test_missing_or_malformed_storage_is_not_created_or_repaired(environment, tmp_path, monkeypatch):
    request = tmp_path / "request.json"
    request.write_text(json.dumps(request_for(tmp_path / "worker")))
    monkeypatch.setattr(kb, "init_db", lambda: pytest.fail("inspection initialized storage"))
    assert invoke(request) != 0
    path = kb.kanban_db_path()
    assert not path.exists()
    path.write_bytes(b"not a sqlite database")
    assert invoke(request) != 0
    assert path.stat().st_size == len(b"not a sqlite database")
    assert not list(environment.glob("*.bak*"))


def test_absence_and_committed_wal_are_observed_without_data_writes(environment, tmp_path):
    kb.init_db()
    path = kb.kanban_db_path()
    request = request_for(tmp_path / "worker")
    with kbc.connect_closing() as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        with readonly_connection(path) as (reader, _):
            empty = observe(reader, request)
            assert empty["outcome"] == "absent" and "task" not in empty
        created = reconcile(writer, request)
        assert Path(str(path) + "-wal").stat().st_size > 0
        before = list(writer.iterdump())
        with readonly_connection(path) as (reader, info):
            result = observe(reader, request)
            assert result["outcome"] == "observed"
            assert result["task"] == created["task"]
            assert result["identity_sha256"] == created["identity_sha256"]
            assert "commit_observed_at_ns" not in result
            assert result["transaction_observed_at_ns"] > 0
            assert info.st_ino == path.stat().st_ino
            assert reader.total_changes == 0 and not reader.in_transaction
            for sql in ["UPDATE tasks SET body='changed'", "CREATE TABLE forbidden(x)",
                        "ATTACH DATABASE ':memory:' AS other", "PRAGMA query_only=OFF",
                        "SELECT load_extension('missing')"]:
                with pytest.raises(sqlite3.DatabaseError):
                    reader.execute(sql)
        assert list(writer.iterdump()) == before


def test_cli_returns_fresh_observation_without_initialization(environment, tmp_path, capsys, monkeypatch):
    kb.init_db()
    request = request_for(tmp_path / "worker")
    file = tmp_path / "request.json"
    file.write_text(json.dumps(request))
    with kbc.connect_closing() as conn:
        created = reconcile(conn, request)
    monkeypatch.setattr(kb, "init_db", lambda: pytest.fail("inspection initialized storage"))
    assert invoke(file) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "observed"
    assert result["task"]["id"] == created["task"]["id"]
    assert result["database"]["path"] == str(kb.kanban_db_path())
    with contextlib.closing(sqlite3.connect(kb.kanban_db_path())) as other:
        other.execute("UPDATE tasks SET status='done'")
        other.commit()
    assert invoke(file) != 0
    output = capsys.readouterr()
    assert not output.out and "not blocked" in output.err


@pytest.mark.parametrize("change", [
    "UPDATE tasks SET body='different'", "UPDATE tasks SET status='archived'",
    "INSERT INTO task_runs(task_id,status,started_at) SELECT id,'done',0 FROM tasks",
    "INSERT INTO task_events(task_id,kind,payload,created_at) SELECT id,'unblocked','{}',0 FROM tasks",
])
def test_mismatch_or_consumed_history_refuses_without_reset(environment, tmp_path, change):
    kb.init_db()
    request = request_for(tmp_path / "worker")
    with kbc.connect_closing() as conn:
        reconcile(conn, request)
        conn.execute(change)
        before = list(conn.iterdump())
        with readonly_connection(kb.kanban_db_path()) as (reader, _):
            with pytest.raises(ValueError):
                observe(reader, request)
            assert not reader.in_transaction
        assert list(conn.iterdump()) == before


def test_observation_refuses_nesting(environment, tmp_path):
    kb.init_db()
    with readonly_connection(kb.kanban_db_path()) as (reader, _):
        reader.execute("BEGIN")
        with pytest.raises(RuntimeError, match="own its read transaction"):
            observe(reader, request_for(tmp_path / "worker"))
        reader.execute("ROLLBACK")


def test_one_snapshot_covers_identity_and_related_history_during_writer_commit(environment, tmp_path):
    kb.init_db()
    path = kb.kanban_db_path()
    request = request_for(tmp_path / "worker")
    with kbc.connect_closing() as setup:
        created = reconcile(setup, request)
    ready, snapshot, committed = threading.Event(), threading.Event(), threading.Event()
    failures = []
    def writer():
        try:
            with kbc.connect_closing() as conn:
                ready.set()
                assert snapshot.wait(15), "reader did not establish a snapshot"
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET body='changed' WHERE id=?", (created["task"]["id"],))
                    kb._append_event(conn, created["task"]["id"], "edited", {"body": "changed"})
        except BaseException as error:
            failures.append(error)
        finally:
            committed.set()
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert ready.wait(15), "writer did not become ready"
        with readonly_connection(path) as (reader, _):
            class ObservedConnection:
                def __getattr__(self, name):
                    return getattr(reader, name)

                def execute(self, sql, parameters=()):
                    cursor = reader.execute(sql, parameters)
                    if sql.startswith("SELECT * FROM tasks"):
                        snapshot.set()
                        assert committed.wait(15), "writer did not commit"
                    return cursor
            old = observe(ObservedConnection(), request)
            assert old["task"] == created["task"]
            assert old["initial_event_ids"] == created["initial_event_ids"]
            with pytest.raises(ValueError, match="identity mismatch: body"):
                observe(reader, request)
        assert not failures
    finally:
        snapshot.set()
        thread.join(20)
        assert not thread.is_alive(), "writer cleanup incomplete"
