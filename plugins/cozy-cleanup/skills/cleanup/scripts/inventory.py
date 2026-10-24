#!/usr/bin/env python3
"""Read-only inventory for the cleanup skill: what on this machine can go, and why.

Prints JSON. It never deletes, pushes, commits or edits anything. Its only write
is `git fetch --prune` per clone that is not active (`--no-fetch` turns it off).
Git reads use `--no-optional-locks`, so they never take a lock another agent needs.

Verdicts, strongest first:
  active        in use, in flight, or changed within --active-hours: leave it
  keep          an ongoing project (keep marker, recent use, recent push, .factory state)
  unsafe        something is on no server remote, or its state is unknown
  backup-first  all git work is on a server remote; local-only ignored files remain
  safe          everything is on a server remote
A copy on another folder of this disk never counts: only remotes on a server do.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
# Ignored folders and files that a build or install makes again. Everything else
# git ignores (.env, local databases, notes) is local-only and goes to a backup.
# Untracked (not ignored) files are never treated as build output.
REGEN_NAMES = {
    "node_modules", "dist", "build", "out", ".astro", ".next", ".nuxt", ".svelte-kit",
    "__pycache__", ".venv", "venv", ".tox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".cache", ".parcel-cache", ".turbo", ".gradle", "target", "coverage", "vendor",
    ".wrangler", ".DS_Store", ".phpunit.cache", ".phpunit.result.cache",
}
REGEN_PATHS = ("storage/framework", "storage/logs", "storage/pail", "bootstrap/cache")
# ponytail: node_modules is never walked for nested repos (size); a repo inside it is missed.
SKIP_WALK = {".git", "node_modules"}
IN_PROGRESS = ("index.lock", "MERGE_HEAD", "rebase-merge", "rebase-apply",
               "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG")
CAP = 200  # files listed per folder; past this the folder itself is listed
ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1",
           GIT_SSH_COMMAND=os.environ.get("GIT_SSH_COMMAND", "ssh -oBatchMode=yes"))


def run(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV)
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


def hosted(url):
    """A remote on a server, not another folder on this disk."""
    return bool(re.match(r"^(https?|ssh|git)://[^/]+/", url or "")
                or re.match(r"^[\w.-]+@[\w.-]+:", url or ""))


def gh_slug(url):
    m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?/?$", url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


def regenerable(rel):
    rel = rel.rstrip("/")
    return (any(part in REGEN_NAMES for part in rel.split("/"))
            or any(("/" + p + "/") in ("/" + rel + "/") for p in REGEN_PATHS))


def find_repos(root, max_depth=None):
    """Git repos under root (root itself if it is one); stops at each repo it finds."""
    if is_repo(root):
        return [root]
    found = []

    def walk(d, depth):
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            if not e.is_dir(follow_symlinks=False) or e.name in SKIP_WALK:
                continue
            if is_repo(e.path):
                found.append(e.path)
            elif max_depth is None or depth < max_depth:
                walk(e.path, depth + 1)

    walk(root, 1)
    return found


def nested_repos(repo):
    """Repos inside repo at any depth, stopping at each (their own nested come from recursion)."""
    found = []
    try:
        entries = list(os.scandir(repo))
    except OSError:
        return found
    for e in entries:
        if e.is_dir(follow_symlinks=False) and e.name not in SKIP_WALK:
            found += [n for n in find_repos(e.path) if not is_linked_worktree(n)]
    return found


def walk_files(repo, full, nested, skip_regen):
    """Files under a folder, outside nested repos (and outside build output when
    skip_regen). Returns (paths relative to repo, capped, newest mtime)."""
    hits, newest = [], 0.0
    if os.path.isfile(full) or os.path.islink(full):
        try:
            newest = os.lstat(full).st_mtime
        except OSError:
            pass
        return [os.path.relpath(full, repo)], False, newest
    for d, dirs, files in os.walk(full):
        dirs[:] = [x for x in dirs if x not in SKIP_WALK and not (skip_regen and x in REGEN_NAMES)
                   and os.path.join(d, x) not in nested]
        for f in files:
            fp = os.path.join(d, f)
            r = os.path.relpath(fp, repo)
            if skip_regen and regenerable(r):
                continue
            try:
                newest = max(newest, os.lstat(fp).st_mtime)
            except OSError:
                pass
            hits.append(r)
            if len(hits) >= CAP:
                return hits, True, newest
    return hits, False, newest


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
    return items  # items[0] is the main working tree (or the bare repo)


def busy_paths():
    """Every path a process has open (files and working folders), plus herdr agent
    folders. None when lsof gives nothing: then nothing can be proved idle."""
    if not shutil.which("lsof"):
        return None
    _, out = run(["lsof", "-w", "-Fn"], timeout=180)
    paths = {os.path.realpath(l[1:]) for l in out.splitlines() if l.startswith("n/")}
    if not paths:
        return None
    if shutil.which("herdr"):
        _, out = run(["herdr", "agent", "list"], timeout=20)
        try:
            for a in json.loads(out)["result"]["agents"]:
                for k in ("cwd", "foreground_cwd"):
                    if a.get(k):
                        paths.add(os.path.realpath(a[k]))
        except (ValueError, KeyError, TypeError):
            pass
    return paths


def other_claude_sessions():
    _, out = run(["ps", "-axo", "pid=,ppid=,command="])
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = (int(parts[1]), parts[2])
    mine, p = set(), os.getpid()
    while p in procs and p not in mine and p > 1:
        mine.add(p)
        p = procs[p][0]
    claude = re.compile(r"(^|/)claude(\s|$)|@anthropic-ai/claude-code|claude-code/cli")
    return sum(1 for pid, (_, cmd) in procs.items() if pid not in mine and claude.search(cmd))


class Ctx:
    def __init__(self, a):
        self.a = a
        self._busy = False  # not taken yet
        self.gh = False
        if not a.no_gh and shutil.which("gh"):
            self.gh = run(["gh", "auth", "status"], timeout=20)[0] == 0
        self._cache = {}

    @property
    def busy(self):
        if self._busy is False:
            self._busy = busy_paths()
        return self._busy

    def gh_json(self, args):
        """Parsed JSON, or None when gh failed (an error is never 'nothing found')."""
        key = tuple(args)
        if key not in self._cache:
            rc, out = run(["gh", *args], timeout=60)
            try:
                self._cache[key] = json.loads(out) if rc == 0 else None
            except ValueError:
                self._cache[key] = None
        return self._cache[key]


def in_use(path, ctx):
    if ctx.busy is None:
        return "cannot tell whether a process uses this folder (lsof gave nothing)"
    real = os.path.realpath(path)
    if any(p == real or p.startswith(real + os.sep) for p in ctx.busy):
        return "a running process or agent has files or its working folder here"
    return None


def keep_marker(repo):
    """git config cleanup.keepUntil (YYYY-MM-DD) / cleanup.keepReason / cleanup.keep."""
    vals = [git(repo, "config", "--get", k)[1].strip()
            for k in ("cleanup.keepUntil", "cleanup.keepReason", "cleanup.keep")]
    until, reason, _ = vals
    if not any(vals):
        return None, None
    if until:
        try:
            if datetime.date.fromisoformat(until) < datetime.date.today():
                return None, f"keep marker expired on {until}"
        except ValueError:
            return f"keep marker with an unreadable date ({until})", None
        return f"marked ongoing until {until}" + (f": {reason}" if reason else ""), None
    return "marked ongoing" + (f": {reason}" if reason else ""), None


def default_branch(repo):
    rc, out = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    return out.strip().split("/", 1)[-1] if rc == 0 else "main"


def hidden_changes(path):
    """skip-worktree / assume-unchanged files whose content differs from git."""
    _, out = git(path, "ls-files", "-v", "-z")
    flagged = [e[2:] for e in out.split("\0") if len(e) > 2 and (e[0] == "S" or e[0].islower())]
    on_disk = [f for f in flagged if os.path.lexists(os.path.join(path, f))][:2000]
    if not on_disk:
        return 0
    _, staged = git(path, "ls-files", "-s", "-z", "--", *on_disk)
    index = {}
    for e in staged.split("\0"):
        if "\t" in e:
            meta, f = e.split("\t", 1)
            index[f] = meta.split()[1]
    p = subprocess.run(["git", "--no-optional-locks", "-C", path, "hash-object", "--stdin-paths"],
                       input="\n".join(on_disk), capture_output=True, text=True, env=ENV)
    return sum(1 for f, h in zip(on_disk, p.stdout.split()) if index.get(f) != h)


def inspect(path, kind, ctx, nested=()):
    a = ctx.a
    rec = {"path": path, "kind": kind, "verdict": None, "active": [], "keep": [], "unsafe": [],
           "local_only": [], "untracked": [], "deps": [], "notes": [], "unpushed": {}}
    rc, gitdir = git(path, "rev-parse", "--absolute-git-dir")
    gitdir = gitdir.strip()
    if rc != 0:
        rec["unsafe"].append("not a readable git repo")
        return rec
    _, common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common = common.strip()

    # Activity first: later git commands must not be what makes it look recent.
    stamps = [os.path.getmtime(os.path.join(gitdir, f)) for f in ("index", "HEAD", "logs/HEAD")
              if os.path.exists(os.path.join(gitdir, f))]
    _, ct = git(path, "log", "-1", "--format=%ct")
    if ct.strip().isdigit():
        stamps.append(int(ct.strip()))

    for f in IN_PROGRESS:
        if os.path.exists(os.path.join(gitdir, f)):
            rec["active"].append(f"git operation in progress ({f})")
    busy = in_use(path, ctx)
    if busy:
        rec["active"].append(busy)

    if not a.no_fetch and kind != "worktree" and not rec["active"]:
        if git(path, "-c", "maintenance.auto=false", "-c", "gc.auto=0",
               "fetch", "--all", "--prune", "--quiet", timeout=300)[0] != 0:
            rec["unsafe"].append("git fetch failed: what is on the remote is unknown")

    _, rv = git(path, "config", "--get-regexp", r"^remote\..*\.url$")
    remotes = {}
    for line in rv.splitlines():
        k, _, url = line.partition(" ")
        remotes[k[len("remote."):-len(".url")]] = url.strip()
    servers = [n for n, u in remotes.items() if hosted(u)]
    for n, u in remotes.items():
        if n not in servers:
            rec["unsafe"].append(f"remote {n} is a folder on this disk ({u}): its copy does not count")
    if not servers:
        rec["unsafe"].append("no remote on a server: nothing here is on GitHub")
    rec["remote"] = remotes.get("origin") or (remotes[servers[0]] if servers else None)
    not_on = [f"--remotes={n}/*" for n in servers]
    slug = next((gh_slug(remotes[n]) for n in sorted(servers, key=lambda n: n != "origin")
                 if gh_slug(remotes[n])), None)
    info = ctx.gh_json(["repo", "view", slug, "--json", "isArchived,pushedAt,visibility"]) if slug and ctx.gh else None
    rec["archived"] = info.get("isArchived") if info else None
    rec["visibility"] = info.get("visibility") if info else None

    # Every commit reachable from a local ref must be on a server remote.
    detached = git(path, "symbolic-ref", "--quiet", "HEAD")[0] != 0
    if kind == "worktree":
        rc, b = git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
        branches = [b.strip()] if rc == 0 else []
        others = ["HEAD"] if detached else []
    else:
        _, out = git(path, "for-each-ref", "--format=%(refname)")
        refs = [r for r in out.split() if not r.startswith("refs/remotes/") and r != "refs/stash"]
        branches = [r[len("refs/heads/"):] for r in refs if r.startswith("refs/heads/")]
        others = [r for r in refs if not r.startswith("refs/heads/")] + (["HEAD"] if detached else [])
    for b in branches:
        rc, n = git(path, "rev-list", "--count", f"refs/heads/{b}", "--not", *not_on, "--")
        if rc != 0:
            rec["unsafe"].append(f"branch {b}: cannot count its commits")
            continue
        if int(n.strip() or 0) == 0:
            continue
        _, tip = git(path, "rev-parse", "--verify", f"refs/heads/{b}")
        if slug and ctx.gh:
            prs = ctx.gh_json(["pr", "list", "-R", slug, "--head", b, "--state", "merged",
                               "--json", "number,headRefOid", "--limit", "50"])
            hit = [p["number"] for p in prs or [] if p.get("headRefOid") == tip.strip()]
            if hit:
                rec["notes"].append(f"branch {b}: on no remote branch, but merged as PR #{hit[0]} at this tip")
                rec.setdefault("merged_branches", []).append(b)
                continue
        rec["unpushed"][b] = int(n.strip())
        rec["unsafe"].append(f"branch {b}: {n.strip()} commit(s) on no server remote"
                             + (" (the GitHub repo is archived)" if rec["archived"] else ""))
    if others:
        rc, n = git(path, "rev-list", "--count", *others, "--not", *not_on, "--")
        if rc != 0 or int(n.strip() or 0) > 0:
            rec["unsafe"].append(f"tags, notes or other refs hold {n.strip() or '?'} commit(s) on no server remote")
    if kind != "worktree" and servers:
        _, local_tags = git(path, "tag", "--list")
        if local_tags.strip():
            if a.no_fetch:
                rec["notes"].append("tag check skipped (--no-fetch)")
            else:
                rc, rt = git(path, "ls-remote", "--tags", servers[0], timeout=120)
                remote_tags = {l.split("refs/tags/", 1)[1].replace("^{}", "")
                               for l in rt.splitlines() if "refs/tags/" in l}
                missing = [t for t in local_tags.split() if t not in remote_tags]
                if rc != 0:
                    rec["unsafe"].append("cannot list the remote's tags")
                elif missing:
                    rec["unsafe"].append(f"{len(missing)} tag(s) not on the remote: {', '.join(missing[:5])}")

    if kind != "worktree" and os.path.isdir(os.path.join(common, "lfs")):
        if not shutil.which("git-lfs"):
            rec["unsafe"].append("an LFS store is here but git-lfs is not installed: cannot check it")
        elif servers:
            rc, out = git(path, "lfs", "push", "--dry-run", servers[0], "--all", timeout=300)
            if rc != 0:
                rec["unsafe"].append("cannot check LFS objects (git lfs push --dry-run failed)")
            elif out.strip():
                rec["unsafe"].append(f"{len(out.splitlines())} LFS object(s) not on the remote")

    feature = [b for b in branches if b != default_branch(path)]
    if feature and not a.no_gh:
        if not (slug and ctx.gh):
            rec["active"].append("cannot check for open PRs (gh missing, not signed in, or not GitHub)")
        else:
            for b in feature:
                prs = ctx.gh_json(["pr", "list", "-R", slug, "--head", b, "--state", "open",
                                   "--json", "number", "--limit", "5"])
                if prs is None:
                    rec["active"].append(f"branch {b}: cannot check for an open PR (gh error)")
                elif prs:
                    rec["active"].append(f"branch {b} has open PR #{prs[0]['number']}")

    if kind != "worktree":
        _, s = git(path, "stash", "list")
        if s.strip():
            rec["unsafe"].append(f"{len(s.splitlines())} stash(es)")
        linked = [w["worktree"] for w in worktrees(path)[1:] if "worktree" in w]
        if linked:
            rec["worktrees"] = linked
            rec["unsafe"].append(f"{len(linked)} linked worktree(s): remove them first")

    hidden = hidden_changes(path)
    if hidden:
        rec["unsafe"].append(f"{hidden} hidden change(s): skip-worktree or assume-unchanged files differ from git")

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
        holds = any(n == full.rstrip("/") or n.startswith(full.rstrip("/") + "/") for n in nested)
        if code == "!!":
            if not holds and regenerable(rel):
                if rel.endswith("/"):
                    rec["deps"].append(full.rstrip("/"))
                continue
            files, capped, newest = walk_files(path, full, nested, skip_regen=True)
            rec["local_only"] += [rel] if capped else files
        elif code == "??":
            files, capped, newest = walk_files(path, full, nested, skip_regen=False)
            rec["untracked"] += [rel] if capped else files
        else:
            dirty.append(rel)
            newest = os.lstat(full).st_mtime if os.path.lexists(full) else 0
        stamps.append(newest)
    if dirty:
        rec["unsafe"].append(f"{len(dirty)} uncommitted change(s) to tracked files")
    if rec["untracked"]:
        rec["unsafe"].append(f"{len(rec['untracked'])} untracked path(s) not in git")

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
        fac = os.path.join(path, ".factory")
        if os.path.isdir(fac) and os.listdir(fac):
            rec["keep"].append("holds orchestrator state in .factory/: its orchestrator retires it")
        if idle_h is not None and idle_h < a.keep_days * 24:
            rec["keep"].append(f"used {idle_h / 24:.0f} day(s) ago (under --keep-days {a.keep_days:g})")
        if slug and not a.no_gh and not info:
            rec["keep"].append("cannot check the last GitHub push (gh missing or failed)")
        elif info and not info.get("isArchived") and info.get("pushedAt"):
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
            rec["unsafe"].append(f"nested repo {rel} holds work on no server remote")
        rec["local_only"] += [os.path.join(rel, f) for f in sub[0]["local_only"]]
    rec["verdict"] = decide(rec)
    return out


def finish(it, sizes=True):
    """Sizes last, so that du does not slow the git checks. A parent's size
    includes its nested repos: do not add both to a total."""
    if sizes and it.get("path") and os.path.exists(it["path"]) and "size_mb" not in it:
        it["size_mb"] = size_mb(it["path"])
    it["deps"] = [d if isinstance(d, dict) else {"path": d, "size_mb": size_mb(d) if sizes else None}
                  for d in it.get("deps", [])]
    return it


def unmounted(path):
    vol = re.match(r"^(/Volumes/[^/]+)", path)
    return bool(vol) and not os.path.ismount(vol.group(1))


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
            items.append({"kind": "install-record", "plugin": pid, "scope": e.get("scope"), "projectPath": pp,
                          "verdict": "keep" if unmounted(pp) else "report-only",
                          "notes": ["the drive is not mounted: the folder may still exist" if unmounted(pp)
                                    else "the folder is gone; the record is harmless"]})
    return items


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", action="append", help="folder to scan (repeatable); default ~/dev and ~/.factory")
    ap.add_argument("--only", help="re-check one clone or worktree (its top folder) right before acting on it")
    ap.add_argument("--active-hours", type=float, default=24, help="changed this recently: active (default 24)")
    ap.add_argument("--keep-days", type=float, default=30, help="used or pushed this recently: ongoing (default 30)")
    ap.add_argument("--no-fetch", action="store_true", help="skip git fetch --prune")
    ap.add_argument("--no-gh", action="store_true", help="skip GitHub checks (PRs, archived, last push)")
    ap.add_argument("--no-plugins", action="store_true", help="skip the Claude Code plugin cache check")
    a = ap.parse_args()
    ctx = Ctx(a)
    others = other_claude_sessions()
    report = {"generated": datetime.datetime.now().isoformat(timespec="seconds"),
              "gh": bool(ctx.gh), "other_claude_sessions": others, "items": []}

    if a.only:
        p = os.path.realpath(os.path.expanduser(a.only))
        rc, top = git(p, "rev-parse", "--show-toplevel")
        if rc != 0 or os.path.realpath(top.strip()) != p:
            print(json.dumps({"error": f"{p} is not the top folder of a clone or worktree: "
                                       "re-check its clone instead"}))
            sys.exit(2)
        if not is_linked_worktree(p):
            rec = inspect_tree(p, "clone", ctx)[0]
        else:
            rec = inspect(p, "worktree", ctx)
            wl = worktrees(p)
            rec["main"] = wl[0].get("worktree") if wl else None
            if any("locked" in w for w in wl if os.path.realpath(w.get("worktree", "")) == p):
                rec["active"].append("worktree is locked (git worktree lock)")
        ctx._busy = False  # a fresh process snapshot, taken last
        busy = in_use(p, ctx)
        if busy and busy not in rec["active"]:
            rec["active"].append(busy)
        rec["verdict"] = decide(rec)
        report["items"] = [finish(rec, sizes=False)]
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
            report["items"] += inspect_tree(repo, "clone", ctx)
            wts += [(w, repo) for w in worktrees(repo)[1:] if "worktree" in w]
    for w, main_repo in wts:
        p = w["worktree"]
        if "prunable" in w or not os.path.isdir(p):
            gone = not unmounted(p)
            report["items"].append({"path": p, "kind": "worktree", "main": main_repo,
                                    "verdict": "safe" if gone else "keep",
                                    "notes": ["the folder is gone; `git worktree prune` drops the record" if gone
                                              else "the drive is not mounted: do not prune"]})
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
