"""Bounded JSON CLI for opt-in exact blocked task reconciliation."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import stat
import sqlite3
import sys


def read_request(path):
    from hermes_cli.kanban_db_blocked_identity import unique_object, validate_request

    request_path = Path(path)
    before = request_path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("request must be a regular nonsymlink file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(request_path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
            raise ValueError("request file changed while opening")
        data = stream.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("request exceeds 2MiB")
    request = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
    validate_request(request)
    return request


def command(args):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_db_blocked_identity import reconcile_blocked_identity

    try:
        request = read_request(args.request_file)
    except OSError as exc:
        raise ValueError("could not read identity request") from exc
    with kbc.connect_closing() as conn:
        result = reconcile_blocked_identity(conn, request)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _read_authorizer(action, first, second, database, trigger):
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_TRANSACTION}
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


@contextlib.contextmanager
def readonly_connection(path):
    """SQL-read-only WAL access, with no repair/migration or raw byte probing."""
    from hermes_cli.sqlite_safe_read import connect_tracked

    path = Path(path)
    if path.resolve() != path or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("observation database must be an existing canonical regular file")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and not sidecar.is_file()):
            raise ValueError("unsupported observation sidecar")
    before = path.stat()
    uri = path.as_uri() + "?mode=ro"
    conn = connect_tracked(uri, tracking_path=path, uri=True, timeout=1, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        disable = getattr(conn, "enable_load_extension", None)
        if callable(disable):
            disable(False)
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(_read_authorizer)
        yield conn, before
        after = path.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("observation database path changed")
    finally:
        conn.close()


def observe_command(args):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_blocked_identity import observe_blocked_identity

    try:
        request = read_request(args.request_file)
        path = kb.kanban_db_path()
        with readonly_connection(path) as (conn, info):
            result = observe_blocked_identity(conn, request)
        result["database"] = {"path": str(path), "device": info.st_dev, "inode": info.st_ino}
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        print("kanban observation refused: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
