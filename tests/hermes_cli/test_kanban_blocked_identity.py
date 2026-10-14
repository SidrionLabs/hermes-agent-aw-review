"""Exact blocked identities refuse changes and consumed history on real boards."""
import contextlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_db_blocked_identity import reconcile_blocked_identity as reconcile


def request_for(workspace):
    return {"version": 1, "identity": {
        "idempotency_key": "selected:task", "title": "Selected task", "body": "exact\nbody\n",
        "assignee": "medijourney-pilot", "created_by": "operator", "workspace_kind": "dir",
        "workspace_path": str(workspace), "skills": ["aw-bounded-coding"],
        "max_runtime_seconds": 900, "max_retries": 1, "model_override": "gpt-6-sol",
        "provider_override": "openai-codex", "completion_contract": "local-only",
        "project_id": None, "priority": 0, "tenant": None, "branch_name": None,
        "reasoning_effort": None, "goal_mode": False, "goal_max_turns": None, "session_id": None,
    }}


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / "board.db"
    kb.init_db(path)
    return path


def test_created_and_existing_are_exact_committed_observations(board, tmp_path):
    request = request_for(tmp_path / "worker")
    with contextlib.closing(kbc.connect(board)) as conn:
        first = reconcile(conn, request)
        assert first["outcome"] == "created" and not conn.in_transaction
        with contextlib.closing(sqlite3.connect(board)) as other:
            assert other.execute("SELECT status FROM tasks WHERE id=?",
                                 (first["task"]["id"],)).fetchone() == ("blocked",)
        before = list(conn.iterdump())
        second = reconcile(conn, request)
        assert second["outcome"] == "existing" and not conn.in_transaction
        assert second["task"] == first["task"]
        assert second["identity_sha256"] == first["identity_sha256"]
        assert second["initial_event_ids"] == first["initial_event_ids"]
        assert list(conn.iterdump()) == before
        for field, value in request["identity"].items():
            assert first["task"][field] == value


@pytest.mark.parametrize("field,value", [
    ("title", "Changed"), ("body", "changed"), ("assignee", "other"),
    ("created_by", "other"), ("workspace_path", "/different"), ("workspace_kind", "scratch"),
    ("skills", '["other"]'), ("max_runtime_seconds", 901), ("max_retries", 2),
    ("model_override", "other"), ("provider_override", "other"),
    ("completion_contract", "other/repo"), ("project_id", "linked"), ("priority", 1),
    ("tenant", "linked"), ("branch_name", "branch"), ("reasoning_effort", "high"),
    ("goal_mode", 1), ("goal_max_turns", 10), ("session_id", "session"),
])
def test_each_field_mismatch_refuses_without_reset(board, tmp_path, field, value):
    request = request_for(tmp_path / "worker")
    with contextlib.closing(kbc.connect(board)) as conn:
        task = reconcile(conn, request)["task"]["id"]
        conn.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (value, task))
        before = list(conn.iterdump())
        with pytest.raises(ValueError, match="identity mismatch"):
            reconcile(conn, request)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("field,value", [
    ("status", "archived"), ("status", "done"), ("status", "ready"),
    ("started_at", 1), ("completed_at", 1), ("claim_lock", "owner"), ("claim_expires", 1),
    ("result", "done"), ("worker_pid", 1), ("worker_started_at", "fingerprint"),
    ("last_failure_error", "error"), ("last_heartbeat_at", 1), ("current_run_id", 1),
    ("workflow_template_id", "workflow"), ("current_step_key", "step"),
    ("block_kind", "human"), ("consecutive_failures", 1), ("block_recurrences", 1),
])
def test_lifecycle_indicators_refuse(board, tmp_path, field, value):
    request = request_for(tmp_path / "worker")
    with contextlib.closing(kbc.connect(board)) as conn:
        task = reconcile(conn, request)["task"]["id"]
        conn.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (value, task))
        before = list(conn.iterdump())
        with pytest.raises(ValueError):
            reconcile(conn, request)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("sql", [
    "INSERT INTO task_runs(task_id,status,started_at) VALUES (?,'done',0)",
    "INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,'actor','note',0)",
    "INSERT INTO task_links(parent_id,child_id) VALUES (?,'child')",
    "INSERT INTO kanban_notify_subs(task_id,platform,chat_id,created_at) VALUES (?,'telegram','chat',0)",
    "INSERT INTO task_attachments(task_id,filename,stored_path,created_at) VALUES (?,'f','/f',0)",
    "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'unblocked','{}',0)",
])
def test_related_history_refuses_even_if_row_still_blocked(board, tmp_path, sql):
    request = request_for(tmp_path / "worker")
    with contextlib.closing(kbc.connect(board)) as conn:
        task = reconcile(conn, request)["task"]["id"]
        conn.execute(sql, (task,))
        before = list(conn.iterdump())
        with pytest.raises(ValueError):
            reconcile(conn, request)
        assert list(conn.iterdump()) == before


def test_archived_plus_new_generic_key_match_is_ambiguous(board, tmp_path):
    request = request_for(tmp_path / "worker")
    with contextlib.closing(kbc.connect(board)) as conn:
        old = reconcile(conn, request)["task"]["id"]
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (old,))
        fresh = kb.create_task(conn, title="generic", idempotency_key="selected:task",
                               workspace_kind="scratch", project_id="")
        assert fresh != old
        before = list(conn.iterdump())
        with pytest.raises(ValueError, match="ambiguous"):
            reconcile(conn, request)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("sql", [
    "UPDATE task_events SET payload='{}' WHERE kind='created'",
    "UPDATE task_events SET run_id=1 WHERE kind='created'",
    "DELETE FROM task_events WHERE kind='blocked'",
    "UPDATE tasks SET skills=NULL",
])
def test_corrupt_initial_evidence_refuses(board, tmp_path, sql):
    request = request_for(tmp_path / "worker")
    with contextlib.closing(kbc.connect(board)) as conn:
        reconcile(conn, request)
        conn.execute(sql)
        before = list(conn.iterdump())
        with pytest.raises(ValueError):
            reconcile(conn, request)
        assert list(conn.iterdump()) == before


def test_committed_observation_does_not_certify_later_eligibility(board, tmp_path):
    request = request_for(tmp_path / "worker")
    with contextlib.closing(kbc.connect(board)) as conn:
        result = reconcile(conn, request)
        with contextlib.closing(kbc.connect(board)) as other:
            assert kb.unblock_task(other, result["task"]["id"])
        assert result["task"]["status"] == "blocked"
        with pytest.raises(ValueError):
            reconcile(conn, request)


def test_board_defaults_are_excluded_and_events_can_cross_seconds(board, tmp_path, monkeypatch):
    monkeypatch.setattr(kb, "_board_meta_for", lambda board: pytest.fail("implicit defaults read"))
    clock = iter([100, 101, 102])
    monkeypatch.setattr(kb, "time", SimpleNamespace(time=lambda: next(clock)))
    with contextlib.closing(kbc.connect(board)) as conn:
        result = reconcile(conn, request_for(tmp_path / "worker"))
        assert result["task"]["created_at"] == 100
        assert [r[0] for r in conn.execute("SELECT created_at FROM task_events ORDER BY id")] == [101, 102]


def test_caller_transaction_refuses_and_commit_failure_rolls_back(board, tmp_path):
    request = request_for(tmp_path / "worker")
    with contextlib.closing(kbc.connect(board)) as conn:
        with kb.write_txn(conn):
            with pytest.raises(RuntimeError, match="already inside"):
                reconcile(conn, request)
        class FailedCommit:
            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, parameters=()):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("injected commit failure")
                return conn.execute(sql, parameters)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            reconcile(FailedCommit(), request)
        assert not conn.in_transaction
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_post_commit_integrity_error_leaves_committed_identity_uncertified(board, tmp_path, monkeypatch):
    request = request_for(tmp_path / "worker")
    with contextlib.closing(kbc.connect(board)) as conn:
        original = kbc._check_file_length_invariant
        def fail_after_outer_commit(connection):
            raise RuntimeError("post-commit integrity observation failed")
        monkeypatch.setattr(kbc, "_check_file_length_invariant", fail_after_outer_commit)
        with pytest.raises(RuntimeError, match="post-commit"):
            reconcile(conn, request)
        monkeypatch.setattr(kbc, "_check_file_length_invariant", original)
        with contextlib.closing(sqlite3.connect(board)) as other:
            assert other.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert reconcile(conn, request)["outcome"] == "existing"


@pytest.mark.parametrize("change", [
    {"project_id": "inherited"}, {"skills": ["aw-bounded-coding", "aw-bounded-coding"]},
    {"max_retries": True}, {"max_runtime_seconds": False}, {"workspace_path": "relative"},
    {"title": " padded "}, {"assignee": "MEDIJOURNEY-PILOT"}, {"unknown": "field"},
])
def test_invalid_request_refuses_without_creating(board, tmp_path, change):
    request = request_for(tmp_path / "worker")
    request["identity"].update(change)
    with contextlib.closing(kbc.connect(board)) as conn:
        with pytest.raises(ValueError):
            reconcile(conn, request)
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
