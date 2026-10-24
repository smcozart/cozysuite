#!/usr/bin/env python3
"""Read-only inventory for the cleanup skill: what on this machine can go, and why.

Prints JSON. It never deletes, pushes, commits or edits anything. The only write
is `git fetch --prune` per clone, which `--no-fetch` turns off. Git reads use
`--no-optional-locks`, so they never take a lock another agent may need.

Verdicts, strongest first:
  active        in use, in flight, or changed within --active-hours: leave it
  keep          an ongoing project (keep marker, recent local use, recent push)
  unsafe        something here is on no remote, or its state is unknown
  backup-first  all git work is on a remote; local-only ignored files remain
  safe          everything here is on a remote
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import time

HOME = os.path.expanduser("~")
# Ignored folders and files that a build or install makes again. Everything else
# that git ignores (.env, local databases, notes) is local-only and is backed up.
REGEN_NAMES = {
    "node_modules", "dist", "build", "out", ".astro", ".next", ".nuxt", ".svelte-kit",
    "__pycache__", ".venv", "venv", ".tox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".cache", ".parcel-cache", ".turbo", ".gradle", "target", "coverage", "vendor",
    ".wrangler", ".DS_Store", ".phpunit.cache", ".phpunit.result.cache",
}
REGEN_PATHS = ("storage/framework", "storage/logs", "storage/pail", "bootstrap/cache")
IN_PROGRESS = ("index.lock", "MERGE_HEAD", "rebase-merge", "rebase-apply",
               "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG")


def run(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1"))
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def git(repo, *args, timeout=120):
    return run(["git", "--no-optional-locks", "-C", repo, *args], timeout=timeout)


def size_mb(path):
    _, out = run(["du", "-sk", path], timeout=900)
    try:
        return round(int(out.split()[0]) / 1024, 1)
    except (IndexError, ValueError):
        return None


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def is_repo(d):
    return os.path.exists(os.path.join(d, ".git"))


def is_linked_worktree(d):
    rc1, gd = git(d, "rev-parse", "--absolute-git-dir")
    rc2, cd = git(d, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return rc1 == 0 and rc2 == 0 and os.path.realpath(gd.strip()) != os.path.realpath(cd.strip())


def regenerable(rel):
    rel = rel.rstrip("/")
    return (any(part in REGEN_NAMES for part in rel.split("/"))
            or any(rel == p or rel.startswith(p + "/") or ("/" + p + "/") in ("/" + rel + "/")
                   for p in REGEN_PATHS))


def find_repos(root, max_depth):
    """Git repos under root; does not descend into a repo it found."""
    found = []

    def walk(d, depth):
        if depth > 0 and is_repo(d):
            found.append(d)
            return
        if depth == max_depth:
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            if e.is_dir(follow_symlinks=False) and e.name != ".git" and e.name not in REGEN_NAMES:
                walk(e.path, depth + 1)

    if is_repo(root):
        return [root]
    walk(root, 0)
    return found


def worktrees(repo):
    _, out = git(repo, "worktree", "list", "--porcelain")
    items, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                items.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        cur[key] = val or True
    return items  # items[0] is the main working tree


def busy_dirs():
    """Working folders of running processes and herdr agents; None if unknown."""
    if not shutil.which("lsof"):
        return None
    _, out = run(["lsof", "-a", "-d", "cwd", "-Fn"], timeout=90)
    dirs = {os.path.realpath(l[1:]) for l in out.splitlines() if l.startswith("n/")}
    if not dirs:
        return None
    if shutil.which("herdr"):
        rc, out = run(["herdr", "agent", "list"], timeout=20)
        try:
            for a in json.loads(out)["result"]["agents"]:
                for k in ("cwd", "foreground_cwd"):
                    if a.get(k):
                        dirs.add(os.path.realpath(a[k]))
        except (ValueError, KeyError, TypeError):
            pass
    return dirs


def other_claude_sessions():
    _, out = run(["ps", "-axo", "pid=,ppid=,comm="])
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = (int(parts[1]), parts[2])
    mine, p = set(), os.getpid()
    while p in procs and p not in mine and p > 1:
        mine.add(p)
        p = procs[p][0]
    return sum(1 for pid, (_, comm) in procs.items()
               if os.path.basename(comm) == "claude" and pid not in mine)


class Ctx:
    def __init__(self, a):
        self.a = a
        self.busy = busy_dirs()
        self.gh = None
        if not a.no_gh and shutil.which("gh"):
            self.gh = run(["gh", "auth", "status"], timeout=20)[0] == 0
        self._cache = {}

    def gh_json(self, args):
        key = tuple(args)
        if key not in self._cache:
            rc, out = run(["gh", *args], timeout=60)
            try:
                self._cache[key] = json.loads(out) if rc == 0 else None
            except ValueError:
                self._cache[key] = None
        return self._cache[key]


def gh_slug(url):
    m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?/?$", url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


def default_branch(repo):
    rc, out = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    return out.strip().split("/", 1)[-1] if rc == 0 else "main"


def nested_repos(repo):
    """Repos inside repo (not linked worktrees: those come from `git worktree list`)."""
    found = []
    try:
        entries = list(os.scandir(repo))
    except OSError:
        return found
    for e in entries:
        if e.is_dir(follow_symlinks=False) and e.name != ".git" and e.name not in REGEN_NAMES:
            found += [n for n in find_repos(e.path, 3) if not is_linked_worktree(n)]
    return found


def loose_files(repo, full, nested):
    """Files under a folder that holds nested repos, outside those repos and build output."""
    hits = []
    for d, dirs, files in os.walk(full):
        dirs[:] = [x for x in dirs if x != ".git" and x not in REGEN_NAMES
                   and os.path.join(d, x) not in nested and not is_repo(os.path.join(d, x))]
        for f in files:
            r = os.path.relpath(os.path.join(d, f), repo)
            if not regenerable(r):
                hits.append(r)
            if len(hits) >= 200:
                return hits
    return hits


def keep_marker(repo):
    """git config cleanup.keepUntil (YYYY-MM-DD) / cleanup.keepReason; local to this clone."""
    _, until = git(repo, "config", "--get", "cleanup.keepUntil")
    _, reason = git(repo, "config", "--get", "cleanup.keepReason")
    _, flag = git(repo, "config", "--get", "cleanup.keep")
    until, reason, flag = until.strip(), reason.strip(), flag.strip()
    if not (until or reason or flag):
        return None, None
    if until:
        try:
            if datetime.date.fromisoformat(until) < datetime.date.today():
                return None, f"keep marker expired on {until}"
        except ValueError:
            return f"keep marker with an unreadable date ({until})", None
        return f"marked ongoing until {until}" + (f": {reason}" if reason else ""), None
    return "marked ongoing" + (f": {reason}" if reason else ""), None


def inspect(path, kind, ctx, nested=()):
    a = ctx.a
    rec = {"path": path, "kind": kind, "verdict": None, "active": [], "keep": [], "unsafe": [],
           "local_only": [], "untracked": [], "deps": [], "notes": [], "unpushed": {}}
    rc, gitdir = git(path, "rev-parse", "--absolute-git-dir")
    gitdir = gitdir.strip()
    if rc != 0:
        rec["unsafe"].append("not a readable git repo")
        return rec

    # Activity first: later git commands must not be what makes it look recent.
    stamps = [os.path.getmtime(os.path.join(gitdir, f)) for f in ("index", "HEAD", "logs/HEAD")
              if os.path.exists(os.path.join(gitdir, f))]
    _, ct = git(path, "log", "-1", "--format=%ct")
    if ct.strip().isdigit():
        stamps.append(int(ct.strip()))

    for f in IN_PROGRESS:
        if os.path.exists(os.path.join(gitdir, f)):
            rec["active"].append(f"git operation in progress ({f})")
    if ctx.busy is None:
        rec["active"].append("cannot tell whether a process works here (lsof gave nothing)")
    else:
        real = os.path.realpath(path)
        if any(d == real or d.startswith(real + os.sep) for d in ctx.busy):
            rec["active"].append("a running process or agent works in this folder")

    if not a.no_fetch and kind != "worktree":
        if git(path, "fetch", "--all", "--prune", "--quiet", timeout=300)[0] != 0:
            rec["unsafe"].append("git fetch failed: what is on the remote is unknown")

    _, remotes = git(path, "remote")
    rc, url = git(path, "remote", "get-url", "origin")
    rec["remote"] = url.strip() if rc == 0 else None
    if not remotes.strip():
        rec["unsafe"].append("no remote: nothing here is on GitHub")
    slug = gh_slug(rec["remote"])
    info = ctx.gh_json(["repo", "view", slug, "--json", "isArchived,pushedAt"]) if slug and ctx.gh else None
    rec["archived"] = info.get("isArchived") if info else None

    if kind == "worktree":
        rc, b = git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
        branches = [b.strip()] if rc == 0 else []
    else:
        _, out = git(path, "for-each-ref", "refs/heads", "--format=%(refname:short)")
        branches = out.split()
    default = default_branch(path)

    for b in branches:
        rc, n = git(path, "rev-list", "--count", f"refs/heads/{b}", "--not", "--remotes", "--")
        if rc != 0:
            rec["unsafe"].append(f"branch {b}: cannot count its commits")
            continue
        if int(n.strip() or 0) == 0:
            continue
        _, tip = git(path, "rev-parse", "--verify", f"refs/heads/{b}")
        if slug and ctx.gh:
            prs = ctx.gh_json(["pr", "list", "-R", slug, "--head", b, "--state", "merged",
                               "--json", "number,headRefOid", "--limit", "50"]) or []
            hit = [p["number"] for p in prs if p.get("headRefOid") == tip.strip()]
            if hit:
                rec["notes"].append(f"branch {b}: on no remote branch, but merged as PR #{hit[0]} at this tip")
                rec.setdefault("merged_branches", []).append(b)
                continue
        rec["unpushed"][b] = int(n.strip())
        rec["unsafe"].append(f"branch {b}: {n.strip()} commit(s) on no remote"
                             + (" (the GitHub repo is archived)" if rec["archived"] else ""))

    if git(path, "symbolic-ref", "--quiet", "HEAD")[0] != 0:
        rc, n = git(path, "rev-list", "--count", "HEAD", "--not", "--remotes", "--branches", "--")
        if rc == 0 and int(n.strip() or 0) > 0:
            rec["unsafe"].append(f"detached HEAD: {n.strip()} commit(s) on no branch or remote")

    feature = [b for b in branches if b != default]
    if feature and slug and ctx.gh:
        for b in feature:
            prs = ctx.gh_json(["pr", "list", "-R", slug, "--head", b, "--state", "open",
                               "--json", "number", "--limit", "5"]) or []
            if prs:
                rec["active"].append(f"branch {b} has open PR #{prs[0]['number']}")
    elif feature and kind == "worktree" and not a.no_gh:
        rec["active"].append("cannot check for an open PR (gh missing, not signed in, or not GitHub)")

    if kind != "worktree":
        _, s = git(path, "stash", "list")
        if s.strip():
            rec["unsafe"].append(f"{len(s.splitlines())} stash(es)")
        linked = [w["worktree"] for w in worktrees(path)[1:]]
        if linked:
            rec["worktrees"] = linked
            rec["unsafe"].append(f"{len(linked)} linked worktree(s): remove them first")

    _, st = git(path, "status", "--porcelain=v1", "-z", "--ignored", "--untracked-files=normal")
    fields = iter(st.split("\0"))
    dirty = []
    for e in fields:
        if len(e) < 4:
            continue
        code, rel = e[:2], e[3:]
        if code[0] in "RC":
            next(fields, None)  # the rename's source path
        full = os.path.join(path, rel)
        holds_nested = any(n == full.rstrip("/") or n.startswith(full.rstrip("/") + "/") for n in nested)
        if code == "!!":
            if regenerable(rel):
                if rel.endswith("/"):
                    rec["deps"].append(full.rstrip("/"))
            elif holds_nested:
                rec["local_only"] += loose_files(path, full, nested)
            else:
                rec["local_only"].append(rel)
        elif code == "??":
            if holds_nested:
                rec["untracked"] += loose_files(path, full, nested)
            elif not regenerable(rel):
                rec["untracked"].append(rel)
        else:
            dirty.append(rel)
    for rel in dirty + rec["untracked"]:
        try:
            stamps.append(os.lstat(os.path.join(path, rel)).st_mtime)
        except OSError:
            pass
    if dirty:
        rec["unsafe"].append(f"{len(dirty)} uncommitted change(s) to tracked files")
    if rec["untracked"]:
        rec["unsafe"].append(f"{len(rec['untracked'])} untracked file(s) not in git")

    idle_h = (time.time() - max(stamps)) / 3600 if stamps else None
    rec["idle_days"] = round(idle_h / 24, 1) if idle_h is not None else None
    if idle_h is not None and idle_h < a.active_hours:
        rec["active"].append(f"changed {idle_h:.1f} h ago (under --active-hours {a.active_hours:g})")

    # Ongoing projects stay: a whole clone only, never a per-ticket worktree.
    if kind != "worktree":
        marker, expired = keep_marker(path)
        if marker:
            rec["keep"].append(marker)
        if expired:
            rec["notes"].append(expired)
        if idle_h is not None and idle_h < a.keep_days * 24:
            rec["keep"].append(f"used {idle_h / 24:.0f} day(s) ago (under --keep-days {a.keep_days:g})")
        if info and not info.get("isArchived") and info.get("pushedAt"):
            pushed = datetime.datetime.strptime(info["pushedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
            days = (datetime.datetime.now(datetime.timezone.utc) - pushed).total_seconds() / 86400
            if days < a.keep_days:
                rec["keep"].append(f"GitHub repo pushed {days:.0f} day(s) ago (under --keep-days {a.keep_days:g})")
    return rec


def decide(rec):
    for v, key in (("active", "active"), ("keep", "keep"), ("unsafe", "unsafe"), ("backup-first", "local_only")):
        if rec.get(key):
            return v
    return "safe"


def inspect_tree(path, kind, ctx, parent=None):
    """A repo plus the repos inside it. Deleting a folder deletes its nested repos,
    so a parent is never better than its worst nested repo."""
    inner = nested_repos(path)
    rec = inspect(path, kind, ctx, nested=set(inner))
    if parent:
        rec["parent"] = parent
    out = [rec]
    for n in inner:
        sub = inspect_tree(n, "nested", ctx, parent=path)
        out += sub
        rel = os.path.relpath(n, path)
        if sub[0]["verdict"] == "active":
            rec["active"].append(f"nested repo {rel} is active")
        if sub[0]["verdict"] == "keep":
            rec["keep"].append(f"nested repo {rel} is an ongoing project")
        if sub[0]["unsafe"]:
            rec["unsafe"].append(f"nested repo {rel} holds work on no remote")
        rec["local_only"] += [os.path.join(rel, f) for f in sub[0]["local_only"]]
    rec["verdict"] = decide(rec)
    return out


def finish(it):
    """Sizes, filled in last so that du does not slow the git checks."""
    if it.get("path") and os.path.exists(it["path"]) and "size_mb" not in it:
        it["size_mb"] = size_mb(it["path"])
    it["deps"] = [d if isinstance(d, dict) else {"path": d, "size_mb": size_mb(d)} for d in it.get("deps", [])]
    return it


def plugin_items(others):
    base = os.path.join(HOME, ".claude", "plugins")
    known = set(load_json(os.path.join(base, "known_marketplaces.json"), {}))
    installed = load_json(os.path.join(base, "installed_plugins.json"), {}).get("plugins", {})
    used = [e.get("installPath", "") for v in installed.values() for e in v]
    items = []
    for sub in ("cache", "marketplaces"):
        d = os.path.join(base, sub)
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            full = os.path.join(d, name)
            if name in known or any(u == full or u.startswith(full + os.sep) for u in used):
                continue
            items.append({"path": full, "kind": "plugin-cache", "size_mb": size_mb(full),
                          "verdict": "active" if others else "safe",
                          "notes": [f"marketplace {name} is not configured and no install uses it"]
                          + ([f"{others} other Claude Code session(s) running: delete later"] if others else [])})
    for pid, entries in installed.items():
        for e in entries:
            pp = e.get("projectPath")
            if not pp or os.path.exists(pp):
                continue
            vol = re.match(r"^(/Volumes/[^/]+)", pp)
            unmounted = bool(vol) and not os.path.ismount(vol.group(1))
            items.append({"kind": "install-record", "plugin": pid, "scope": e.get("scope"), "projectPath": pp,
                          "verdict": "keep" if unmounted else "report-only",
                          "notes": ["the drive is not mounted: the folder may still exist" if unmounted
                                    else "the folder is gone; the record is harmless"]})
    return items


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", action="append", help="folder to scan (repeatable); default ~/dev and ~/.factory")
    ap.add_argument("--only", help="inspect this one path again: the re-check right before acting on it")
    ap.add_argument("--active-hours", type=float, default=24, help="changed this recently: active (default 24)")
    ap.add_argument("--keep-days", type=float, default=30, help="used or pushed this recently: an ongoing project (default 30)")
    ap.add_argument("--no-fetch", action="store_true", help="skip git fetch --prune")
    ap.add_argument("--no-gh", action="store_true", help="skip GitHub checks (merged and open PRs, archived, last push)")
    ap.add_argument("--no-plugins", action="store_true", help="skip the Claude Code plugin cache check")
    a = ap.parse_args()
    ctx = Ctx(a)
    others = other_claude_sessions()
    report = {"generated": datetime.datetime.now().isoformat(timespec="seconds"),
              "gh": bool(ctx.gh), "other_claude_sessions": others, "items": []}

    if a.only:
        p = os.path.abspath(os.path.expanduser(a.only))
        if not is_linked_worktree(p):
            rec = inspect_tree(p, "clone", ctx)[0]
        else:
            rec = inspect(p, "worktree", ctx)
            _, common = git(p, "rev-parse", "--path-format=absolute", "--git-common-dir")
            rec["main"] = os.path.dirname(common.strip().rstrip("/"))
            if any("locked" in w for w in worktrees(rec["main"])
                   if os.path.realpath(w.get("worktree", "")) == os.path.realpath(p)):
                rec["active"].append("worktree is locked (git worktree lock)")
            rec["verdict"] = decide(rec)
        report["items"] = [finish(rec)]
        print(json.dumps(report, indent=1))
        return

    roots = [os.path.abspath(os.path.expanduser(r)) for r in (a.root or ["~/dev", "~/.factory"])]
    report["roots"] = [r for r in roots if os.path.isdir(r)]
    seen, wts = set(), []
    for r in report["roots"]:
        for repo in find_repos(r, 3):
            if os.path.realpath(repo) in seen or is_linked_worktree(repo):
                continue
            seen.add(os.path.realpath(repo))
            recs = inspect_tree(repo, "clone", ctx)
            report["items"] += recs
            for w in worktrees(repo)[1:]:
                wts.append((w, repo))
    for w, main_repo in wts:
        p = w["worktree"]
        if "prunable" in w or not os.path.isdir(p):
            report["items"].append({"path": p, "kind": "worktree", "main": main_repo, "verdict": "safe",
                                    "notes": ["the folder is gone; `git worktree prune` drops the record"]})
            continue
        rec = inspect(p, "worktree", ctx)
        rec["main"] = main_repo
        if "locked" in w:
            rec["active"].append("worktree is locked (git worktree lock)")
        rec["verdict"] = decide(rec)
        report["items"].append(rec)
    report["items"] = [finish(it) for it in report["items"]]
    if not a.no_plugins:
        report["items"] += plugin_items(others)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
