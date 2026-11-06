#!/usr/bin/env python3
"""Self-check for inventory.py: throwaway repos, one per case. Offline: the server
remote https://github.com/test/repo.git is rewritten (url.insteadOf) to a local
bare repo, so it counts as a server while a plain folder remote does not.
Everything is aged two years, then the fresh cases are made.
Run: python3 test_inventory.py, or pytest (the same test). For macOS, Linux and Windows."""
import datetime
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True  # no __pycache__ left in the plugin
import inventory as inv  # noqa: E402
URL = "https://github.com/test/repo.git"
OLD = time.time() - 2 * 365 * 86400


def sh(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def commit(repo, name, push=None):
    write(os.path.join(repo, name), name)
    sh("git", "-C", repo, "add", "--", name)
    sh("git", "-C", repo, "commit", "-qm", name)
    if push:
        sh("git", "-C", repo, "push", "-q", "origin", push)


def exclude(repo, pattern):
    with open(os.path.join(repo, ".git", "info", "exclude"), "a") as f:
        f.write(pattern + "\n")


def age(top):
    nofollow = os.utime in os.supports_follow_symlinks  # not on Windows
    for d, dirs, files in os.walk(top):
        for n in dirs + files:
            f = os.path.join(d, n)
            if nofollow or not os.path.islink(f):
                os.utime(f, (OLD, OLD), **({"follow_symlinks": False} if nofollow else {}))


def idle(path, cwd):
    """A process that sits in cwd, holding path open when one is given."""
    code = "import sys, time\nf = open(sys.argv[1]) if len(sys.argv) > 1 else None\ntime.sleep(300)"
    return subprocess.Popen([sys.executable, "-c", code] + ([path] if path else []), cwd=cwd)


def rmtree(top):
    def writable(fn, path, _):  # Windows: git objects are read-only
        os.chmod(path, stat.S_IWRITE)
        fn(path)
    shutil.rmtree(top, **({"onexc": writable} if sys.version_info >= (3, 12) else {"onerror": writable}))


def plugin_checks(t):
    """Plugin caches: unreadable records or uncounted sessions never come out safe."""
    home, bad = os.path.join(t, "home"), []
    base = os.path.join(home, ".claude", "plugins")
    write(os.path.join(base, "cache", "gone", "x"))
    write(os.path.join(base, "known_marketplaces.json"), "{}")
    write(os.path.join(base, "installed_plugins.json"), '{"plugins": {}}')
    inv.HOME = home
    try:
        for others, want in ((0, "safe"), (2, "active"), (None, "active")):
            got = [i["verdict"] for i in inv.plugin_items(others)]
            if got != [want]:
                bad.append(f"plugin cache with {others} other sessions: want {want}, got {got}")
        with open(os.path.join(base, "installed_plugins.json"), "wb") as f:
            f.write(b'{"plugins": {"\xff": []}}')  # not UTF-8
        got = [i["verdict"] for i in inv.plugin_items(0)]
        if got != ["active"]:
            bad.append(f"plugin cache with unreadable records: want active, got {got}")
    finally:
        inv.HOME = os.path.expanduser("~")
    if not isinstance(inv.other_claude_sessions(), int):
        bad.append("other_claude_sessions: cannot count processes on this OS")
    return bad


def inventory(*args):
    p = subprocess.run([sys.executable, os.path.join(HERE, "inventory.py"), "--no-gh", "--no-plugins", *args],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return p.returncode, None
    return 0, {os.path.realpath(i["path"]): i for i in json.loads(p.stdout)["items"]}


def test_inventory():
    t = os.path.realpath(tempfile.mkdtemp(prefix="cleanup-test-"))
    remote, dev = os.path.join(t, "remote.git"), os.path.join(t, "dev")
    cfg = os.path.join(t, "gitconfig")
    write(cfg, f'[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n'
               f'[url "{remote.replace(os.sep, "/")}"]\n\tinsteadOf = {URL}\n')  # git reads a backslash as an escape
    os.environ.update(GIT_CONFIG_GLOBAL=cfg, GIT_CONFIG_NOSYSTEM="1",
                      GIT_AUTHOR_DATE="2024-01-01T00:00:00", GIT_COMMITTER_DATE="2024-01-01T00:00:00")
    procs = []
    try:
        sh("git", "init", "-q", "--bare", remote)
        seed = os.path.join(t, "seed")
        sh("git", "clone", "-q", URL, seed)
        commit(seed, "README")
        commit(seed, "a.txt")
        blob = subprocess.run(["git", "-C", seed, "hash-object", "-w", "--stdin"], input="README",
                              capture_output=True, text=True, check=True).stdout.strip()
        sh("git", "-C", seed, "update-index", "--add", "--cacheinfo", f"120000,{blob},link")  # a symlink, also on Windows
        sh("git", "-C", seed, "commit", "-qm", "link")
        sh("git", "-C", seed, "push", "-q", "origin", "main")
        p = {k: os.path.join(dev, k) for k in "ABCDEFGHIJKLMNPQRSUVWXYZ"}
        for k in p:
            sh("git", "clone", "-q", URL, p[k])
        sh("git", "clone", "-q", remote, os.path.join(dev, "O"))  # remote is a folder on this disk
        p["O"] = os.path.join(dev, "O")

        commit(p["B"], "unpushed")
        write(os.path.join(p["C"], "README"), "changed")
        sh("git", "-C", p["C"], "stash", "-q")
        write(os.path.join(p["D"], "notes.txt"), "work")
        exclude(p["E"], ".env")
        write(os.path.join(p["E"], ".env"), "KEY=1")
        exclude(p["F"], "node_modules/")
        write(os.path.join(p["F"], "node_modules", "x.js"))
        w1, w2 = os.path.join(dev, "wt1"), os.path.join(dev, "wt2")
        sh("git", "-C", p["G"], "worktree", "add", "-q", "-b", "feat1", w1)
        commit(w1, "feat1", push="feat1")
        sh("git", "-C", p["G"], "worktree", "add", "-q", "-b", "feat2", w2)
        write(os.path.join(w2, "README"), "dirty")
        exclude(p["H"], "apps/")
        inner = os.path.join(p["H"], "apps", "inner")
        sh("git", "clone", "-q", URL, inner)
        commit(inner, "inner-commit")
        future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        sh("git", "-C", p["K"], "config", "cleanup.keepUntil", future)
        sh("git", "-C", p["K"], "config", "cleanup.keepReason", "client work")
        sh("git", "-C", p["L"], "config", "cleanup.keepUntil", "2001-01-01")
        write(os.path.join(p["M"], "src", "build", "feature.py"), "work")  # untracked, build-named folder
        commit(p["N"], "tagged")
        sh("git", "-C", p["N"], "tag", "t1")
        sh("git", "-C", p["N"], "reset", "-q", "--hard", "origin/main")  # the commit lives on the tag only
        sh("git", "-C", p["P"], "update-index", "--skip-worktree", "README")
        write(os.path.join(p["P"], "README"), "hidden edit")
        exclude(p["Q"], "data/")
        sh("git", "clone", "-q", URL, os.path.join(p["Q"], "data", "repo"))
        for i in range(300):
            write(os.path.join(p["Q"], "data", f"f{i}.csv"))
        write(os.path.join(p["R"], ".factory", "reservations.json"), "{}")
        exclude(p["R"], ".factory/")
        exclude(p["S"], ".env")
        exclude(p["U"], "out/")
        deep = os.path.join(p["U"], "out", "a", "b", "c", "deep")
        sh("git", "clone", "-q", URL, deep)
        commit(deep, "deep-commit")
        write(os.path.join(p["V"], "log.txt"), "open")
        sh("git", "-C", p["W"], "update-index", "--skip-worktree", "a.txt", "README")
        write(os.path.join(p["W"], "a.txt"), "hidden edit")
        os.chmod(os.path.join(p["W"], "README"), 0)  # listed first and cannot be hashed: not clean
        sh("git", "-C", p["X"], "update-index", "--skip-worktree", "link")  # unedited symlink: clean
        sh("git", "-C", p["Y"], "tag", "t2")  # a tag name the remote does not have
        write(os.path.join(p["Z"], "caf\u00e9 notes.txt"), "work")  # untracked, a name that is not ASCII

        age(dev)
        # Fresh cases, after aging.
        a2 = os.path.join(dev, "A2")
        sh("git", "clone", "-q", URL, a2)
        write(os.path.join(p["S"], ".env"), "written now")
        write(os.path.join(p["I"], ".git", "index.lock"), "")
        procs.append(idle(None, cwd=p["J"]))
        procs.append(idle(os.path.join(p["V"], "log.txt"), cwd=t))
        time.sleep(1)

        rc, got = inventory("--root", dev)
        assert rc == 0, "inventory failed"
        want = {"A": "safe", "B": "unsafe", "C": "unsafe", "D": "unsafe", "E": "backup-first", "F": "safe",
                "G": "unsafe", "H": "unsafe", "I": "active", "J": "active", "K": "keep", "L": "safe",
                "M": "unsafe", "N": "unsafe", "O": "unsafe", "P": "unsafe", "Q": "backup-first", "R": "keep",
                "S": "active", "U": "unsafe", "V": "active", "W": "unsafe", "X": "safe", "Y": "unsafe", "Z": "unsafe",
                "A2": "active"}
        p["A2"] = a2
        bad = [f"{k}: want {v}, got {got[p[k]]['verdict']} {got[p[k]]['active'] + got[p[k]]['unsafe']}"
               for k, v in want.items() if got[p[k]]["verdict"] != v]
        for path, v in ((w1, "safe"), (w2, "unsafe"), (inner, "unsafe"), (deep, "unsafe")):
            if got.get(path, {}).get("verdict") != v:
                bad.append(f"{path}: want {v}, got {got.get(path, {}).get('verdict')}")
        checks = [
            (got[w1]["kind"] == "worktree" and got[inner]["kind"] == "nested", "worktree and nested kinds"),
            (got[p["E"]]["local_only"] == [".env"], f"E local_only {got[p['E']]['local_only']}"),
            ([d["path"] for d in got[p["F"]]["deps"]] == [os.path.join(p["F"], "node_modules")], "F deps"),
            (any("expired" in n for n in got[p["L"]]["notes"]), "L expired note"),
            (got[p["B"]]["unpushed"] == {"main": 1}, f"B unpushed {got[p['B']]['unpushed']}"),
            (got[p["M"]]["untracked"] == ["src/build/feature.py"], f"M untracked {got[p['M']]['untracked']}"),
            (got[p["H"]]["local_only"] == [], f"H: nested repo files listed as local_only {got[p['H']]['local_only']}"),
            (got[p["Z"]]["untracked"] == ["caf\u00e9 notes.txt"], f"Z untracked {got[p['Z']]['untracked']}"),
            (any("tag" in u for u in got[p["N"]]["unsafe"]), "N tag reason"),
            (any("folder on this disk" in u for u in got[p["O"]]["unsafe"]), "O local remote reason"),
            (any("hidden change" in u for u in got[p["P"]]["unsafe"]), "P hidden change reason"),
            ("data/" in got[p["Q"]]["local_only"], f"Q capped folder {got[p['Q']]['local_only'][:3]}"),
        ]
        bad += [msg for ok, msg in checks if not ok]
        rc, _ = inventory("--only", os.path.join(p["F"], "node_modules"))
        if rc != 2:
            bad.append(f"--only on a deps folder: want exit 2, got {rc}")
        rc, one = inventory("--only", p["Y"], "--no-fetch")
        if not one or one[os.path.realpath(p["Y"])]["verdict"] != "unsafe":
            bad.append("--only Y --no-fetch: a local-only tag must stay unsafe")
        rc, one = inventory("--only", w1)
        if not one or one[w1]["verdict"] != "safe":
            bad.append("--only wt1: want safe")
        bad += plugin_checks(t)
        assert not bad, "\n".join(bad)
        print(f"ok: {len(want) + 4 + len(checks) + 3 + 5} checks")
    finally:
        for pr in procs:
            pr.kill()
            pr.wait()  # Windows: a folder a process sits in cannot be deleted
        if "W" in locals().get("p", {}):
            os.chmod(os.path.join(p["W"], "README"), 0o644)
        rmtree(t)


if __name__ == "__main__":
    test_inventory()
