"""Verify the frozen AW-043 candidate on an isolated Linux Actions runner."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

BASE = "ae3598c7cdf3a2e4a6f60d0d869602813dded97a"
CANDIDATE = "e12235075953429b853c4957af868b6d6a02cd0a"
FILES = {
    "hermes_cli/kanban_db.py": "2b15c822fbf40440502a6bf8d66001abc51f8c750c5010cb53f1fd4b9aa39b75",
    "hermes_cli/kanban_db_connect.py": "a84f75d437ef27a9eb33ea9ce6555b0db30dbefdd95828bcd025878657eff10a",
    "hermes_cli/kanban_db_identity.py": "ac29d1b740bb17c0d88f91989d9709eb76b5d97b44405e516a7a318ded73c25f",
    "tests/hermes_cli/test_kanban_atomic_identity.py": "2a2bbf2bd35503c3279edaa4e9c3e393f11bab2b132ece1bbe1adc05684cb5cc",
    "tests/hermes_cli/test_kanban_db.py": "97add28ae1bace339835f0f9b2ee1a6d3956cdf792a4a8100b671baf37acb201",
}


def verify_bytes(root):
    for name, expected in FILES.items():
        actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError("candidate bytes differ: " + name)


def selected_tests(root):
    primary = {str(p.relative_to(root))
               for parent in ("tests/hermes_cli", "tests/tools")
               for p in root.glob(parent + "/test_kanban*.py")}
    primary.update({"tests/tools/test_delegate_kanban_isolation.py",
                    "tests/hermes_cli/test_pin_kanban_board_env.py",
                    "tests/hermes_cli/test_signal_handler_kanban_worker.py"})
    surfaces = {str(p.relative_to(root))
                for parent in ("tests/plugins", "tests/gateway", "tests/agent",
                               "tests/cron", "tests/tui_gateway")
                for p in root.glob(parent + "/test_*kanban*.py")}
    if len(primary) != 86 or len(surfaces) != 34:
        raise ValueError("frozen test selection differs")
    return sorted(primary | surfaces)


def main():
    root = Path(sys.argv[1]).resolve()
    evidence = Path(os.environ["RUNNER_TEMP"]) / "aw043-linux-evidence"
    evidence.mkdir(exist_ok=True)
    checks = []
    result = {"candidate": CANDIDATE, "base": BASE,
              "status": "failed/unverified", "whole_runtime_suite": "unrun",
              "windows": "unrun", "installed_adoption": False}

    def check(label, argv, timeout=1200):
        print(label + ": " + json.dumps(argv), flush=True)
        with (evidence / (label + ".log")).open("w") as log:
            try:
                proc = subprocess.run(argv, cwd=root, stdout=log,
                                      stderr=subprocess.STDOUT, timeout=timeout)
                exit_code, timed_out = proc.returncode, False
            except subprocess.TimeoutExpired:
                exit_code, timed_out = None, True
        checks.append({"label": label, "argv": argv, "exit_code": exit_code,
                       "timeout": timed_out})
        (evidence / "checks.json").write_text(json.dumps(checks, indent=2))
        if timed_out or exit_code != 0:
            raise RuntimeError(label + " failed; later checks unrun; no retry")
        return (evidence / (label + ".log")).read_text().strip()

    try:
        if sys.platform != "linux":
            raise ValueError("Linux runner required")
        verify_bytes(root)
        if check("head", ["git", "rev-parse", "HEAD"]) != CANDIDATE:
            raise ValueError("candidate commit differs")
        if check("parent", ["git", "rev-parse", "HEAD^"]) != BASE:
            raise ValueError("upstream parent differs")
        changed = check("changed-files", ["git", "diff", "--name-only", BASE, CANDIDATE])
        if set(changed.splitlines()) != set(FILES):
            raise ValueError("candidate file scope differs")
        python = os.environ["HERMES_PYTHON"]
        check("interpreter", [python, "-c",
              "import sys,sqlite3,hermes_state_wal as w; "
              "print(sys.version,sqlite3.sqlite_version); "
              "assert sys.version_info[:2] == (3,14); "
              "assert not w.is_sqlite_wal_reset_vulnerable()"])
        paths = selected_tests(root)
        (evidence / "selected-tests.json").write_text(json.dumps(paths, indent=2))
        check("linux-kanban", ["/bin/bash", "scripts/run_tests.sh", "-j", "2",
              "--file-retries", "0", "--file-timeout", "600"] + paths + ["--tb=short"], 1800)
        check("lint-health", [python, "scripts/check", "--base", BASE])
        check("compilation", [python, "-m", "py_compile"] + list(FILES))
        check("whitespace", ["git", "diff", "--check", BASE, CANDIDATE])
        if check("tracked-mutations", ["git", "diff", "--name-only", "HEAD"]):
            raise ValueError("tracked files changed during verification")
        if check("untracked-files", ["git", "ls-files", "--others", "--exclude-standard"]):
            raise ValueError("unexpected untracked candidate files")
        check("final-status", ["git", "status", "--short", "--branch"])
        verify_bytes(root)
        result["status"] = "selected Linux Kanban checks passed"
    except Exception as exc:
        result["error"] = str(exc)
        print("STOP: " + str(exc), flush=True)
    finally:
        (evidence / "result.json").write_text(json.dumps(result, indent=2))
    return 0 if result["status"] == "selected Linux Kanban checks passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
