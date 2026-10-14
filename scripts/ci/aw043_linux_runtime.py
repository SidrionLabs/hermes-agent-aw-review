"""Run one default native test slice against the frozen review candidate."""
import json
import os
from pathlib import Path
import subprocess
import sys

from aw043_linux_review import BASE, CANDIDATE, verify_bytes


def main():
    root = Path(sys.argv[1]).resolve()
    evidence = Path(os.environ["RUNNER_TEMP"]) / "aw043-runtime-evidence"
    evidence.mkdir(exist_ok=True)
    checks = []
    result = {"candidate": CANDIDATE, "base": BASE,
              "slice": os.environ["HERMES_TEST_SLICE"],
              "status": "failed/unverified", "suite": "native default discovery",
              "integration_e2e_docker": "excluded by native default discovery",
              "macos_windows": "unrun", "installed_adoption": False}

    def check(label, argv, timeout=300):
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
            raise ValueError("candidate parent differs")
        check("interpreter", [os.environ["HERMES_PYTHON"], "-c",
              "import sys,sqlite3,hermes_state_wal as w; "
              "print(sys.version,sqlite3.sqlite_version); "
              "assert sys.version_info[:2] == (3,14); "
              "assert not w.is_sqlite_wal_reset_vulnerable()"])
        check("native-default-runtime", ["/bin/bash", "scripts/run_tests.sh",
              "-j", "2", "--file-retries", "0", "--file-timeout", "600"], 3000)
        if check("tracked-mutations", ["git", "diff", "--name-only", "HEAD"]):
            raise ValueError("tracked files changed during verification")
        if check("untracked-files", ["git", "ls-files", "--others", "--exclude-standard"]):
            raise ValueError("unexpected untracked candidate files")
        verify_bytes(root)
        result["status"] = "native default Linux slice passed"
    except Exception as exc:
        result["error"] = str(exc)
        print("STOP: " + str(exc), flush=True)
    finally:
        (evidence / "result.json").write_text(json.dumps(result, indent=2))
    return 0 if result["status"] == "native default Linux slice passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
