"""Opt-in exact reconciliation for independent, never-started blocked tasks.

Generic creation remains key-based. This operation owns its outer transaction
so its result cannot confuse an inner savepoint with a durable observation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time


_TEXT_FIELDS = (
    "idempotency_key", "title", "body", "assignee", "created_by",
    "workspace_path", "model_override", "provider_override",
)
_FIXED_FIELDS = {
    "workspace_kind": "dir", "max_retries": 1, "completion_contract": "local-only",
    "project_id": None, "priority": 0, "tenant": None, "branch_name": None,
    "reasoning_effort": None, "goal_mode": False, "goal_max_turns": None,
    "session_id": None,
}
_NULL_STATE = (
    "started_at", "completed_at", "claim_lock", "claim_expires", "result",
    "worker_pid", "worker_started_at", "last_failure_error", "last_heartbeat_at",
    "current_run_id", "workflow_template_id", "current_step_key", "block_kind",
)
_ZERO_STATE = ("consecutive_failures", "block_recurrences")


def unique_object(pairs):
    """Duplicate JSON fields are ambiguous requests or audit payloads."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field: " + key)
        result[key] = value
    return result


def _validate_text(identity):
    for field in _TEXT_FIELDS:
        value = identity[field]
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("invalid text: " + field)
        if field != "body" and (not value or value.strip() != value):
            raise ValueError("noncanonical text: " + field)


def _validate_runtime(identity):
    for field, expected in _FIXED_FIELDS.items():
        if type(identity[field]) is not type(expected) or identity[field] != expected:
            raise ValueError("unsupported identity value: " + field)
    runtime = identity["max_runtime_seconds"]
    if type(runtime) is not int or not 1 <= runtime <= 1800:
        raise ValueError("runtime must be an integer from 1 to 1800")
    path = identity["workspace_path"]
    if not Path(path).is_absolute() or os.path.normpath(path) != path:
        raise ValueError("workspace path must be absolute and lexically canonical")


def _validate_assignment(identity):
    from hermes_cli import kanban_db as kb

    if kb._canonical_assignee(identity["assignee"]) != identity["assignee"]:
        raise ValueError("assignee must be canonical")
    skills = identity["skills"]
    if (not isinstance(skills, list) or not skills
            or any(not isinstance(s, str) or not s or "\0" in s for s in skills)
            or kb._normalize_task_skills(skills) != skills):
        raise ValueError("skills must be a nonempty canonical ordered list")
    model = identity["model_override"], identity["provider_override"]
    if kb._validate_model_override(*model) != model:
        raise ValueError("model/provider must be canonical")


def validate_request(request):
    if not isinstance(request, dict) or set(request) != {"version", "identity"}:
        raise ValueError("expected version and identity")
    if type(request["version"]) is not int or request["version"] != 1:
        raise ValueError("unsupported identity request version")
    identity = request["identity"]
    fields = set(_TEXT_FIELDS) | set(_FIXED_FIELDS) | {"skills", "max_runtime_seconds"}
    if not isinstance(identity, dict) or set(identity) != fields:
        raise ValueError("identity fields differ")
    _validate_text(identity)
    _validate_runtime(identity)
    _validate_assignment(identity)
    # Detach nested lists from the caller before taking the writer lock.
    return json.loads(json.dumps(identity, ensure_ascii=False))


def _initial_events(conn, row, identity):
    events = conn.execute(
        "SELECT id,run_id,kind,payload,created_at FROM task_events "
        "WHERE task_id=? ORDER BY id LIMIT 3", (row["id"],),
    ).fetchall()
    created = {
        "assignee": identity["assignee"], "status": "blocked", "parents": [],
        "creator_task_id": None, "tenant": None, "workspace_kind": "dir",
        "workspace_path": identity["workspace_path"], "branch_name": None,
        "project_id": None, "skills": identity["skills"], "goal_mode": None,
        "model_override": identity["model_override"],
        "provider_override": identity["provider_override"],
    }
    blocked = {"reason": "initial_status", "status": "blocked",
               "actor": identity["created_by"]}
    if len(events) != 2 or [e["kind"] for e in events] != ["created", "blocked"]:
        raise ValueError("identity has noninitial lifecycle history")
    previous_time = row["created_at"]
    for event, expected in zip(events, (created, blocked)):
        if (event["run_id"] is not None or type(event["created_at"]) is not int
                or event["created_at"] < previous_time):
            raise ValueError("identity has noninitial event metadata")
        previous_time = event["created_at"]
        if (not isinstance(event["payload"], str)
                or json.loads(event["payload"], object_pairs_hook=unique_object) != expected):
            raise ValueError("identity has noninitial event payload")
    return [e["id"] for e in events]


def _inspect_identity(conn, row, identity):
    expected_columns = (set(identity) | set(_NULL_STATE) | set(_ZERO_STATE)
                        | {"id", "status", "created_at"})
    if set(row.keys()) != expected_columns:
        raise ValueError("unsupported task schema")
    actual = dict(row)
    if not isinstance(actual["skills"], str) or type(row["created_at"]) is not int:
        raise ValueError("malformed identity row")
    actual["skills"] = json.loads(actual["skills"])
    for field, expected in identity.items():
        if actual[field] != expected:
            raise ValueError("identity mismatch: " + field)
    if row["status"] != "blocked":
        raise ValueError("identity is not blocked")
    if (any(row[f] is not None for f in _NULL_STATE)
            or any(row[f] != 0 for f in _ZERO_STATE)):
        raise ValueError("identity has lifecycle state")
    for table in ("task_runs", "task_comments", "task_attachments", "kanban_notify_subs"):
        if conn.execute(f"SELECT 1 FROM {table} WHERE task_id=? LIMIT 1", (row["id"],)).fetchone():
            raise ValueError("identity has related history: " + table)
    if conn.execute("SELECT 1 FROM task_links WHERE parent_id=? OR child_id=? LIMIT 1",
                    (row["id"], row["id"])).fetchone():
        raise ValueError("identity has dependency links")
    events = _initial_events(conn, row, identity)
    actual["goal_mode"] = False
    return actual, events


def reconcile_blocked_identity(conn, request):
    """Create or observe one exact identity; return only after owned commit.

    Inspect all surviving key matches, including archived rows. Purged history
    is not recoverable here: callers still need a durable independent attempt
    ledger and must never interpret this operation as authorization to replay.
    """
    from hermes_cli import kanban_db as kb

    identity = validate_request(request)
    with kb.write_txn(conn):
        rows = conn.execute("SELECT * FROM tasks WHERE idempotency_key=? LIMIT 2",
                            (identity["idempotency_key"],)).fetchall()
        if len(rows) > 1:
            raise ValueError("ambiguous historical identity")
        outcome = "existing"
        if not rows:
            inputs = dict(identity)
            inputs["project_id"] = ""  # Explicit native board-project opt-out.
            task_id = kb.create_task(conn, **inputs, initial_status="blocked")
            rows = [conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()]
            outcome = "created"
        task, events = _inspect_identity(conn, rows[0], identity)
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        result = {"version": 1, "outcome": outcome, "task": task,
                  "identity_sha256": hashlib.sha256(encoded).hexdigest(),
                  "initial_event_ids": events}
    # write_txn's post-commit integrity check can still raise: no result then.
    result["commit_observed_at_ns"] = time.time_ns()
    return result


def observe_blocked_identity(conn, request):
    """One SQL read snapshot; no creation, initialization or eligibility lease."""
    identity = validate_request(request)
    if conn.in_transaction:
        raise RuntimeError("observation must own its read transaction")
    conn.execute("BEGIN")
    try:
        rows = conn.execute("SELECT * FROM tasks WHERE idempotency_key=? LIMIT 2",
                            (identity["idempotency_key"],)).fetchall()
        if len(rows) > 1:
            raise ValueError("ambiguous historical identity")
        result = {"version": 1, "outcome": "absent"}
        if rows:
            task, events = _inspect_identity(conn, rows[0], identity)
            result.update(outcome="observed", task=task, initial_event_ids=events)
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        result["identity_sha256"] = hashlib.sha256(encoded).hexdigest()
        result["transaction_observed_at_ns"] = time.time_ns()
    finally:
        conn.execute("ROLLBACK")
    return result
