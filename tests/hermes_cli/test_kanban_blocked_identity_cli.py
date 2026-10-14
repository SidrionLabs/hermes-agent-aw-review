"""Real parser/handler boundaries for exact identity JSON and ownership."""
import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc, kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_db_blocked_identity import reconcile_blocked_identity
from tests.hermes_cli.test_kanban_blocked_identity import request_for


@pytest.fixture
def environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def invoke(path):
    parser = argparse.ArgumentParser()
    kc.build_parser(parser.add_subparsers())
    args = parser.parse_args(["kanban", "reconcile-blocked", "--request-file", str(path)])
    return kc.kanban_command(args)


def test_real_cli_creates_observes_and_refuses_changed_body(environment, tmp_path, capsys):
    request = request_for(tmp_path / "worker")
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    assert invoke(path) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["outcome"] == "created"
    assert invoke(path) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "existing"
    request["identity"]["body"] = "different"
    path.write_text(json.dumps(request))
    assert invoke(path) != 0
    output = capsys.readouterr()
    assert not output.out and "identity mismatch: body" in output.err
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT body FROM tasks").fetchone()[0] == first["task"]["body"]


@pytest.mark.parametrize("content", [
    '{"version":1,"version":1,"identity":{}}', '{"version":1,"identity":{}}',
    '{bad json', 'x' * (2 * 1024 * 1024 + 1),
])
def test_malformed_duplicate_and_oversized_requests_do_not_create(environment, tmp_path, content):
    path = tmp_path / "request.json"
    path.write_text(content)
    assert invoke(path) != 0
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.platforms("posix")
def test_symlink_and_missing_request_refuse(environment, tmp_path):
    target = tmp_path / "request.json"
    target.write_text(json.dumps(request_for(tmp_path / "worker")))
    alias = tmp_path / "alias.json"
    alias.symlink_to(target)
    assert invoke(alias) != 0
    assert invoke(tmp_path / "missing.json") != 0
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_delegated_child_refuses_cli_and_direct_operation(environment, tmp_path, monkeypatch):
    request = request_for(tmp_path / "worker")
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    with kbc.connect_closing() as conn:
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
        assert invoke(path) != 0
        with pytest.raises(PermissionError, match="delegate_task"):
            reconcile_blocked_identity(conn, request)
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_stdout_loss_after_commit_keeps_card_for_manual_reconciliation(environment, tmp_path, monkeypatch):
    from hermes_cli.kanban_blocked_identity import command

    path = tmp_path / "request.json"
    path.write_text(json.dumps(request_for(tmp_path / "worker")))
    def lost_response(*args, **kwargs):
        raise BrokenPipeError("injected stdout loss")
    with monkeypatch.context() as scope:
        scope.setattr("builtins.print", lost_response)
        with pytest.raises(BrokenPipeError):
            command(argparse.Namespace(request_file=str(path)))
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0
