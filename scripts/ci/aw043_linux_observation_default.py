"""Verify one native Linux default slice of the frozen exact-identity composition."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from aw043_linux_observation import (
    ALL_FILES as IMPLEMENTATION_FILES, BASE, CANDIDATE as IMPLEMENTATION, PARENT,
)

CANDIDATE = "a2444e56f3e0a5960a7180a996672583511fc3fa"
FIXTURES = {
    "tests/hermes_cli/test_shared_metrics_install_run.py": "0859223d7203c505e453db245a00892085f4435811b9e45a0ec527a780f2cf96",
    "tests/plugins/test_telegram_update_admission.py": "885460e7b2a2e7d24df302c3a2da6a6e403c5bda457becec7c3284cd91da6c74",
}
FILES = {**IMPLEMENTATION_FILES, **FIXTURES}


def admit(root):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
    if (git("rev-parse", "HEAD") != CANDIDATE
            or git("rev-parse", "HEAD^") != IMPLEMENTATION
            or git("rev-parse", "HEAD^^") != PARENT
            or git("rev-parse", "HEAD^^^") != "e12235075953429b853c4957af868b6d6a02cd0a"
            or git("rev-parse", "HEAD^^^^") != BASE):
        raise ValueError("exact composition ancestry differs")
    if set(git("diff", "--name-only", IMPLEMENTATION, CANDIDATE).splitlines()) != set(FIXTURES):
        raise ValueError("fixture-only composition scope differs")
    if set(git("diff", "--name-only", BASE, CANDIDATE).splitlines()) != set(FILES):
        raise ValueError("full sixteen-file source scope differs")
    if git("status", "--porcelain"):
        raise ValueError("source checkout dirty")
    for name, expected in FILES.items():
        path = root / name
        if (path.is_symlink() or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            raise ValueError("source bytes differ: " + name)
    return {"candidate": CANDIDATE, "implementation": IMPLEMENTATION,
            "prerequisite": PARENT, "upstream": BASE, "sha256": FILES}


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
    result = {"candidate": CANDIDATE, "implementation": IMPLEMENTATION,
              "base": BASE, "slice": os.environ["HERMES_TEST_SLICE"],
              "status": "failed/unverified", "suite": "native default discovery",
              "integration_e2e_docker": "excluded by native default discovery",
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
        check("interpreter", [python, "-c",
              "import sys,sqlite3,hermes_state_wal as w; "
              "print(sys.version,sqlite3.sqlite_version); "
              "assert sys.version_info[:2] == (3,14); "
              "assert not w.is_sqlite_wal_reset_vulnerable()"])
        check("native-default-runtime", ["/bin/bash", "scripts/run_tests.sh",
              "-j", "2", "--file-retries", "0", "--file-timeout", "600"], 3000)
        if check("tracked-mutations", ["git", "diff", "--name-only", "HEAD"]):
            raise ValueError("tracked source changed")
        if check("untracked-files", ["git", "ls-files", "--others", "--exclude-standard"]):
            raise ValueError("unexpected untracked source")
        admit(root)
        result["status"] = "native default Linux slice passed"
    except Exception as exc:
        result["error"] = str(exc)
        print("STOP: " + str(exc), flush=True)
    finally:
        (evidence / "result.json").write_text(json.dumps(result, indent=2))
    return 0 if result["status"] == "native default Linux slice passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
