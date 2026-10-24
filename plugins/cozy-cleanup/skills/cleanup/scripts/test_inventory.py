#!/usr/bin/env python3
"""Self-check for inventory.py: throwaway repos, one per case, against a local bare remote.
Offline. Run: python3 test_inventory.py"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def sh(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def clone(remote, path):
    sh("git", "clone", "-q", remote, path)
    sh("git", "-C", path, "config", "user.email", "t@example.com")
    sh("git", "-C", path, "config", "user.name", "t")


def commit(path, name, push=False, branch=None):
    open(os.path.join(path, name), "w").write(name)
    sh("git", "-C", path, "add", name)
    sh("git", "-C", path, "commit", "-qm", name)
    if push:
        sh("git", "-C", path, "push", "-q", "origin", branch or "HEAD")


def inventory(*args):
    out = subprocess.run([sys.executable, os.path.join(HERE, "inventory.py"), "--no-gh", "--no-plugins", *args],
                         check=True, capture_output=True, text=True).stdout
    return {os.path.realpath(i["path"]): i for i in json.loads(out)["items"]}


def main():
    t = os.path.realpath(tempfile.mkdtemp(prefix="cleanup-test-"))
    remote, dev = os.path.join(t, "remote.git"), os.path.join(t, "dev")
    os.makedirs(dev)
    sh("git", "init", "-q", "--bare", "-b", "main", remote)
    seed = os.path.join(t, "seed")
    clone(remote, seed)
    commit(seed, "README", push=True, branch="main")
    p = {k: os.path.join(dev, k) for k in "ABCDEFGHIJKL"}
    for k in p:
        clone(remote, p[k])
    commit(p["B"], "local-only-commit")
    open(os.path.join(p["C"], "README"), "a").write("x")
    sh("git", "-C", p["C"], "stash", "-q")
    open(os.path.join(p["D"], "notes.txt"), "w").write("work")
    for k, pat, f in (("E", ".env", ".env"), ("F", "node_modules/", "node_modules/x.js")):
        open(os.path.join(p[k], ".git", "info", "exclude"), "a").write(pat + "\n")
        os.makedirs(os.path.dirname(os.path.join(p[k], f)), exist_ok=True)
        open(os.path.join(p[k], f), "w").write("x")
    w1, w2 = os.path.join(dev, "wt1"), os.path.join(dev, "wt2")
    sh("git", "-C", p["G"], "worktree", "add", "-q", "-b", "feat1", w1)
    commit(w1, "feat1", push=True, branch="feat1")
    sh("git", "-C", p["G"], "worktree", "add", "-q", "-b", "feat2", w2)
    open(os.path.join(w2, "README"), "a").write("dirty")
    open(os.path.join(p["H"], ".git", "info", "exclude"), "a").write("apps/\n")
    inner = os.path.join(p["H"], "apps", "inner")
    clone(remote, inner)
    commit(inner, "inner-commit")
    open(os.path.join(p["I"], ".git", "index.lock"), "w").close()
    busy = subprocess.Popen(["sleep", "120"], cwd=p["J"])
    future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    sh("git", "-C", p["K"], "config", "cleanup.keepUntil", future)
    sh("git", "-C", p["K"], "config", "cleanup.keepReason", "client work")
    sh("git", "-C", p["L"], "config", "cleanup.keepUntil", "2001-01-01")
    try:
        got = inventory("--root", dev, "--active-hours", "0", "--keep-days", "0")
        want = {"A": "safe", "B": "unsafe", "C": "unsafe", "D": "unsafe", "E": "backup-first",
                "F": "safe", "G": "unsafe", "H": "unsafe", "I": "active", "J": "active", "K": "keep", "L": "safe"}
        bad = [f"{k}: want {v}, got {got[p[k]]['verdict']}" for k, v in want.items() if got[p[k]]["verdict"] != v]
        for path, v in ((w1, "safe"), (w2, "unsafe"), (inner, "unsafe")):
            if got[path]["verdict"] != v:
                bad.append(f"{path}: want {v}, got {got[path]['verdict']}")
        assert got[w1]["kind"] == "worktree" and got[inner]["kind"] == "nested", "worktree/nested kinds"
        assert got[p["E"]]["local_only"] == [".env"], got[p["E"]]["local_only"]
        assert [d["path"] for d in got[p["F"]]["deps"]] == [os.path.join(p["F"], "node_modules")], got[p["F"]]["deps"]
        assert any("expired" in n for n in got[p["L"]]["notes"]), got[p["L"]]["notes"]
        assert got[p["B"]]["unpushed"] == {"main": 1}, got[p["B"]]["unpushed"]
        # Defaults: a clone changed seconds ago is active; with --active-hours 0 it is an ongoing project.
        if inventory("--only", p["A"])[os.path.realpath(p["A"])]["verdict"] != "active":
            bad.append("A with default --active-hours: want active")
        if inventory("--only", p["A"], "--active-hours", "0")[os.path.realpath(p["A"])]["verdict"] != "keep":
            bad.append("A with default --keep-days: want keep")
        if inventory("--only", w1, "--active-hours", "0")[w1]["verdict"] != "safe":
            bad.append("--only wt1: want safe")
        assert not bad, "\n".join(bad)
        print("ok: 18 checks")
    finally:
        busy.kill()
        shutil.rmtree(t, ignore_errors=True)


if __name__ == "__main__":
    main()
