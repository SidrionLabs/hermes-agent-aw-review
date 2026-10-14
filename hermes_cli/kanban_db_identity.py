"""Task identity lookup composed inside the caller's write transaction."""
import sqlite3


def active_task_id(conn: sqlite3.Connection, key: str) -> str | None:
    """Return one active identity; refuse inconsistent historical duplicates."""
    rows = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? "
        "AND status != 'archived' LIMIT 2", (key,),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("ambiguous task idempotency identity")
    return rows[0]["id"] if rows else None
