"""Independent-process exact and mixed generic creators on disposable boards."""
import contextlib
import json
import select
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tests.hermes_cli.test_kanban_blocked_identity import request_for


_CHILD = """
import contextlib, json, sys
from pathlib import Path
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_db_blocked_identity import reconcile_blocked_identity
request = json.loads(Path(sys.argv[2]).read_text())
with contextlib.closing(kbc.connect(Path(sys.argv[1]))) as conn:
    print('ready', flush=True)
    assert sys.stdin.readline().strip() == 'create'
    try:
        if sys.argv[3] == 'generic':
            inputs = dict(request['identity']); inputs['project_id'] = ''
            result = {'task': {'id': kb.create_task(conn, **inputs, initial_status='blocked')}}
        else:
            result = reconcile_blocked_identity(conn, request)
    except ValueError as error:
        result = {'refused': str(error)}
    print(json.dumps(result), flush=True)
"""


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("second", ["exact", "generic", "mismatch"])
def test_independent_creators_do_not_reset_or_duplicate(tmp_path, monkeypatch, second):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    board = tmp_path / "board.db"
    kb.init_db(board)
    requests = []
    for index in range(2):
        request = request_for(tmp_path / "worker")
        if index == 1 and second == "mismatch":
            request["identity"]["body"] = "different"
        path = tmp_path / f"request-{index}.json"
        path.write_text(json.dumps(request))
        requests.append(path)
    children = []
    try:
        for index, path in enumerate(requests):
            mode = "generic" if index == 1 and second == "generic" else "exact"
            children.append(subprocess.Popen(
                [sys.executable, "-c", _CHILD, str(board), str(path), mode],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        for child in children:
            assert select.select([child.stdout], [], [], 15)[0], "child readiness timed out"
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            child.stdin.write("create\n")
            child.stdin.flush()
        results = []
        for child in children:
            output, errors = child.communicate(timeout=20)
            assert child.returncode == 0, errors
            results.append(json.loads(output))
        with contextlib.closing(kbc.connect(board)) as conn:
            rows = conn.execute("SELECT id,body FROM tasks").fetchall()
            assert len(rows) == 1
            assert conn.execute("SELECT count(*) FROM task_events WHERE kind='created'").fetchone()[0] == 1
            if second == "mismatch":
                winners = [r for r in results if "task" in r]
                refusals = [r for r in results if "refused" in r]
                assert len(winners) == len(refusals) == 1
                assert "identity mismatch: body" in refusals[0]["refused"]
                assert rows[0]["body"] == winners[0]["task"]["body"]
            else:
                assert results[0]["task"]["id"] == results[1]["task"]["id"] == rows[0]["id"]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate()
