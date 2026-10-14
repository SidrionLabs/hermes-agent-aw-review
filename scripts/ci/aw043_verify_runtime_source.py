"""Verify the immutable review source before provisioning native test dependencies."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

BASE = "ae3598c7cdf3a2e4a6f60d0d869602813dded97a"
PARENT = "e12235075953429b853c4957af868b6d6a02cd0a"
HEAD = "2581a6325da28288b471d8c83b0d9e35f173ab12"
FILES = {
    "hermes_cli/kanban_db.py": "2b15c822fbf40440502a6bf8d66001abc51f8c750c5010cb53f1fd4b9aa39b75",
    "hermes_cli/kanban_db_connect.py": "a84f75d437ef27a9eb33ea9ce6555b0db30dbefdd95828bcd025878657eff10a",
    "hermes_cli/kanban_db_identity.py": "ac29d1b740bb17c0d88f91989d9709eb76b5d97b44405e516a7a318ded73c25f",
    "tests/hermes_cli/test_kanban_atomic_identity.py": "2a2bbf2bd35503c3279edaa4e9c3e393f11bab2b132ece1bbe1adc05684cb5cc",
    "tests/hermes_cli/test_kanban_db.py": "97add28ae1bace339835f0f9b2ee1a6d3956cdf792a4a8100b671baf37acb201",
    "tests/hermes_cli/test_shared_metrics_install_run.py": "0859223d7203c505e453db245a00892085f4435811b9e45a0ec527a780f2cf96",
    "tests/plugins/test_telegram_update_admission.py": "885460e7b2a2e7d24df302c3a2da6a6e403c5bda457becec7c3284cd91da6c74",
}


def verify_source(root, head=HEAD, parent=PARENT, base=BASE, files=FILES):
    """Expected identities are fixed by the workflow; parameters support disposable checks."""
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True, timeout=30).stdout.strip()

    observed = {"head": git("rev-parse", "HEAD"),
                "parent": git("rev-parse", "HEAD^"),
                "base": git("rev-parse", "HEAD^^")}
    if observed != {"head": head, "parent": parent, "base": base}:
        raise ValueError("reviewed commit ancestry differs")
    paths = git("diff", "--name-only", base, head).splitlines()
    if set(paths) != set(files):
        raise ValueError("reviewed file scope differs")
    if git("status", "--porcelain"):
        raise ValueError("source checkout contains unexpected changes")
    hashes = {}
    for name, expected in files.items():
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("nonregular reviewed source file: " + name)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            raise ValueError("reviewed source bytes differ: " + name)
        hashes[name] = digest
    return {**observed, "files": hashes, "status": "reviewed source verified"}


if __name__ == "__main__":
    try:
        print(json.dumps(verify_source(Path(sys.argv[1]).resolve()), indent=2))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("STOP: source admission failed: " + str(error), file=sys.stderr)
        raise SystemExit(1)
