"""Verify the frozen opt-in exact blocked-identity extension on Linux."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from aw043_linux_review import BASE, FILES as BASE_FILES

PARENT = "e12235075953429b853c4957af868b6d6a02cd0a"
CANDIDATE = "77271a457975ac7a061b922773e0c33cdcb33638"
CHANGED = {
    "hermes_cli/kanban.py": "4912ada0d94da73051291d33ce9218f19598f0c9d38204161a3c0ccc714e296b",
    "hermes_cli/kanban_blocked_identity.py": "15c61f72efd633dddbac091a0bc9920704161d1db95e6a24b9d651e9cbbecf61",
    "hermes_cli/kanban_db_blocked_identity.py": "2d339501d989891d96c777c08372c5a89b38439e776f1f72cb8afb50a6a15be5",
    "hermes_cli/kanban_parser.py": "9b92c6724294c38fdadf538b72b121114a2d7133ae4e7cca4df46ad8abb17adf",
    "website/docs/developer-guide/kanban-blocked-identity.md": "ac79104f26eef8495612d58e67c6d8a8779580dc38ea87978c89b2617a0f6a70",
    "tests/hermes_cli/test_kanban_blocked_identity_concurrency.py": "e06c27c9f3d09522e673aac72b7fff82ff61e65af09f1272bf9c1633df488c48",
    "tests/hermes_cli/test_kanban_blocked_identity_cli.py": "a9446b1047ab5760908d56632abce544396c0287172c8f447707fb8d43abd1fd",
    "tests/hermes_cli/test_kanban_blocked_identity.py": "14ea06dc1a4da6a960abb2a3ea92c062c118ccef61dfa0ce6dae51342ff5ebec"
}
ALL_FILES = {**BASE_FILES, **CHANGED}


def admit(root):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
    if (git("rev-parse", "HEAD") != CANDIDATE
            or git("rev-parse", "HEAD^") != PARENT
            or git("rev-parse", "HEAD^^") != BASE):
        raise ValueError("candidate ancestry differs")
    if set(git("diff", "--name-only", PARENT, CANDIDATE).splitlines()) != set(CHANGED):
        raise ValueError("eight-file successor scope differs")
    if set(git("diff", "--name-only", BASE, CANDIDATE).splitlines()) != set(ALL_FILES):
        raise ValueError("full prerequisite/successor scope differs")
    if git("status", "--porcelain"):
        raise ValueError("source checkout dirty")
    for name, expected in ALL_FILES.items():
        path = root / name
        if (path.is_symlink() or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            raise ValueError("source bytes differ: " + name)
    return {"head": CANDIDATE, "parent": PARENT, "base": BASE, "sha256": ALL_FILES}


def selected_tests(root):
    paths = {str(p.relative_to(root)) for parent in ("tests/hermes_cli", "tests/tools")
             for p in root.glob(parent + "/test_kanban*.py")}
    paths.update({"tests/tools/test_delegate_kanban_isolation.py",
                  "tests/hermes_cli/test_pin_kanban_board_env.py",
                  "tests/hermes_cli/test_signal_handler_kanban_worker.py"})
    paths.update(str(p.relative_to(root))
                 for parent in ("tests/plugins", "tests/gateway", "tests/agent",
                                "tests/cron", "tests/tui_gateway")
                 for p in root.glob(parent + "/test_*kanban*.py"))
    if len(paths) != 123:
        raise ValueError("frozen selected caller set differs")
    return sorted(paths)


def main():
    root = Path(sys.argv[1]).resolve()
    evidence = Path(os.environ["RUNNER_TEMP"]) / "aw043-runtime-evidence"
    evidence.mkdir(exist_ok=True)
    source = admit(root)
    (evidence / "source-admission.json").write_text(json.dumps(source, indent=2))
    if "--admit-only" in sys.argv:
        print(json.dumps(source, indent=2))
        return 0
    checks = []
    result = {"candidate": CANDIDATE, "parent": PARENT, "base": BASE,
              "status": "failed/unverified", "whole_runtime_suite": "unrun",
              "macos_windows": "unrun", "installed_adoption": False}

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
            raise ValueError("native Linux runner required")
        python = os.environ["HERMES_PYTHON"]
        # Verify the interpreter environment before the first behavioral check.
        check("interpreter", [python, "-c",
              "import sys,sqlite3,hermes_state_wal as w; "
              "print(sys.version,sqlite3.sqlite_version); "
              "assert sys.version_info[:2] == (3,14); "
              "assert not w.is_sqlite_wal_reset_vulnerable()"])
        focused = sorted(p for p in CHANGED if p.startswith("tests/"))
        check("focused-exact-identity", ["/bin/bash", "scripts/run_tests.sh", "-j", "2",
              "--file-retries", "0", "--file-timeout", "300", *focused, "--tb=short"], 900)
        selected = selected_tests(root)
        (evidence / "selected-tests.json").write_text(json.dumps(selected, indent=2))
        check("selected-kanban-callers", ["/bin/bash", "scripts/run_tests.sh", "-j", "2",
              "--file-retries", "0", "--file-timeout", "600", *selected, "--tb=short"], 1800)
        check("lint-health", [python, "scripts/check", "--base", BASE])
        check("compilation", [python, "-m", "py_compile",
              *sorted(p for p in ALL_FILES if p.endswith(".py"))])
        check("whitespace", ["git", "diff", "--check", BASE, CANDIDATE])
        if check("tracked-mutations", ["git", "diff", "--name-only", "HEAD"]):
            raise ValueError("tracked source changed")
        if check("untracked-files", ["git", "ls-files", "--others", "--exclude-standard"]):
            raise ValueError("unexpected untracked source")
        admit(root)
        result["status"] = "exact identity focused/caller checks passed"
    except Exception as exc:
        result["error"] = str(exc)
        print("STOP: " + str(exc), flush=True)
    finally:
        (evidence / "result.json").write_text(json.dumps(result, indent=2))
    return 0 if result["status"] == "exact identity focused/caller checks passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
