"""Bounded JSON CLI for opt-in exact blocked task reconciliation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat


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
