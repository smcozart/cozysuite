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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
WINDOWS = os.name == "nt"
ROOTS_FILE = os.path.join(HOME, ".config", "cozy-cleanup", "roots")  # one folder per line
DEFAULT_ROOTS = ["~/dev", "~/.factory"]
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
        p = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="surrogateescape",
                           timeout=timeout, env=ENV)
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


def load_json(path):
    """Parsed JSON, or None when the file is missing or unreadable (never an empty default)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def norm(path):
    """One spelling per folder: links resolved; on Windows also one case and one separator."""
    return os.path.normcase(os.path.realpath(path))


def key(path):
    """One spelling per path without touching the disk (git's / and Windows' backslash alike)."""
    return os.path.normcase(os.path.normpath(path))


def posix(rel):
    return rel.replace(os.sep, "/")


def is_repo(d):
    return os.path.exists(os.path.join(d, ".git"))


def is_linked_worktree(d):
    rc1, gd = git(d, "rev-parse", "--absolute-git-dir")
    rc2, cd = git(d, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return rc1 == 0 and rc2 == 0 and norm(gd.strip()) != norm(cd.strip())


def hosted(url):
    """A remote on a server, not another folder on this disk."""
    # ponytail: reads the raw URL; an insteadOf that maps it to a local mirror is not seen.
    m = re.match(r"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?([^/:]+)[^/]*/|[\w.-]+@([\w.-]+):)", url or "")
    return bool(m) and (m.group(1) or m.group(2)) not in ("localhost", "127.0.0.1", "::1")


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
    skip_regen; nested holds key() spellings). Returns (paths relative to repo, with /,
    capped, newest mtime). A path git named that cannot be read is listed as it is."""
    hits, newest, capped, seen = [], 0.0, False, 0
    if not os.path.isdir(full) or os.path.islink(full):
        try:
            newest = os.lstat(full).st_mtime
        except OSError:
            pass
        return [posix(os.path.relpath(full, repo))], False, newest
    for d, dirs, files in os.walk(full):
        dirs[:] = [x for x in dirs if x not in SKIP_WALK and not (skip_regen and x in REGEN_NAMES)
                   and key(os.path.join(d, x)) not in nested]
        for f in files:
            fp = os.path.join(d, f)
            r = posix(os.path.relpath(fp, repo))
            if skip_regen and regenerable(r):
                continue
            try:
                newest = max(newest, os.lstat(fp).st_mtime)
            except OSError:
                pass
            if len(hits) < CAP:
                hits.append(r)
            else:
                capped = True
            seen += 1
            if seen >= 5000:  # ponytail: dating stops at 5000 files per folder
                return hits, capped, newest
    return hits, capped, newest


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


def open_paths():
    """Every path a process has open: files, folders and working folders.
    Returns (paths, None), or (None, why) when they cannot be read."""
    if WINDOWS:
        try:
            return windows_open_paths(), None
        except (OSError, StopIteration, ValueError) as e:
            return None, f"cannot read open handles: {e or type(e).__name__}"
    if not shutil.which("lsof"):
        return None, "lsof is not installed"
    _, out = run(["lsof", "-w", "-n", "-P", "-Fn"], timeout=180)
    paths = {l[1:] for l in out.splitlines() if l.startswith("n/")}
    return (paths, None) if paths else (None, "lsof gave nothing")


def windows_open_paths():
    """Windows has no lsof. One snapshot of the system handle table, then the path of
    every file handle in each process we may read: open files, folders held open, and
    working folders (a process's working folder, and Git Bash's, is an open handle).
    ponytail: another user's or an elevated process is not seen, as with lsof without sudo."""
    import ctypes
    import msvcrt
    import queue
    import threading
    from ctypes import wintypes as w

    class Entry(ctypes.Structure):  # SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX
        _fields_ = [("obj", ctypes.c_void_p), ("pid", ctypes.c_size_t), ("handle", ctypes.c_size_t),
                    ("access", ctypes.c_ulong), ("trace", ctypes.c_ushort), ("type", ctypes.c_ushort),
                    ("attrs", ctypes.c_ulong), ("reserved", ctypes.c_ulong)]

    ntdll, k32 = ctypes.WinDLL("ntdll"), ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                               ctypes.POINTER(ctypes.c_ulong)]
    ntdll.NtQuerySystemInformation.restype = ctypes.c_ulong
    k32.OpenProcess.restype = w.HANDLE
    k32.GetCurrentProcess.restype = w.HANDLE
    k32.DuplicateHandle.argtypes = [w.HANDLE, w.HANDLE, w.HANDLE, ctypes.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD]
    k32.GetFileType.argtypes = [w.HANDLE]
    k32.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
    k32.CloseHandle.argtypes = [w.HANDLE]

    me, fd = os.getpid(), os.open(sys.executable, os.O_RDONLY)  # a file handle of ours: its type number
    try:
        size = 1 << 22
        while True:
            buf, need = ctypes.create_string_buffer(size), ctypes.c_ulong()
            status = ntdll.NtQuerySystemInformation(64, buf, size, ctypes.byref(need))  # extended handles
            if status != 0xC0000004:  # not STATUS_INFO_LENGTH_MISMATCH
                break
            size = max(size * 2, need.value + (1 << 20))
        if status != 0:
            raise OSError(f"NtQuerySystemInformation status {status:#x}")
        n = ctypes.c_size_t.from_buffer(buf).value
        table = (Entry * n).from_buffer(buf, 2 * ctypes.sizeof(ctypes.c_size_t))
        mine = msvcrt.get_osfhandle(fd)
        file_type = next(e.type for e in table if e.pid == me and e.handle == mine)
    finally:
        os.close(fd)
    by_pid = {}
    for e in table:
        if e.type == file_type:  # ours too: its working folder counts, as with lsof
            by_pid.setdefault(e.pid, []).append(e.handle)

    def worker(jobs, results):  # a query can block on a pipe with a pending read: run it aside
        out = ctypes.create_unicode_buffer(32768)
        while True:
            h = jobs.get()
            n = k32.GetFinalPathNameByHandleW(h, out, 32768, 0) if k32.GetFileType(h) == 1 else 0  # disk
            results.put(out.value if 0 < n < 32768 else None)

    jobs, results = queue.Queue(), queue.Queue()
    threading.Thread(target=worker, args=(jobs, results), daemon=True).start()
    this, paths = k32.GetCurrentProcess(), set()
    for pid, handles in by_pid.items():
        proc = k32.OpenProcess(0x40, False, pid)  # PROCESS_DUP_HANDLE
        if not proc:
            continue
        try:
            for h in handles:
                dup = w.HANDLE()
                if not k32.DuplicateHandle(proc, h, this, ctypes.byref(dup), 0, False, 2):  # SAME_ACCESS
                    continue
                jobs.put(dup.value)
                try:
                    p = results.get(timeout=2)
                except queue.Empty:  # an unknown path must not read as "nothing open"
                    raise OSError(f"a handle of process {pid} could not be read") from None
                k32.CloseHandle(dup)
                if p:  # the \\?\ form: \\?\C:\x is C:\x, \\?\UNC\host\x is \\host\x
                    paths.add("\\\\" + p[8:] if p.startswith("\\\\?\\UNC\\") else p[4:] if p.startswith("\\\\?\\") else p)
        finally:
            k32.CloseHandle(proc)
    if not paths:
        raise OSError("no open file handles found")
    return paths


def busy_paths():
    """(normalized paths in use, None), or (None, why): then nothing can be proved idle.
    Adds herdr agent folders: an agent between commands may hold nothing open."""
    paths, why = open_paths()
    if paths is None:
        return None, why
    if shutil.which("herdr"):
        _, out = run(["herdr", "agent", "list"], timeout=20)
        try:
            for a in json.loads(out)["result"]["agents"]:
                for k in ("cwd", "foreground_cwd"):
                    if a.get(k):
                        paths.add(a[k])
        except (ValueError, KeyError, TypeError):
            pass
    return {norm(p) for p in paths}, None


def process_table():
    """{pid: (ppid, command line)} for every process, or None when it cannot be read."""
    procs = {}
    if WINDOWS:  # Git Bash's ps sees only its own processes; CIM sees all (names always)
        _, out = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                      "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-CimInstance Win32_Process | "
                      "Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"],
                     timeout=60)
        try:
            for p in json.loads(out):
                procs[int(p["ProcessId"])] = (int(p["ParentProcessId"]), p["CommandLine"] or p["Name"] or "")
        except (ValueError, KeyError, TypeError):
            return None
    else:
        _, out = run(["ps", "-axo", "pid=,ppid=,command="])
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                procs[int(parts[0])] = (int(parts[1]), parts[2])
    return procs or None


def other_claude_sessions():
    """Claude Code processes outside this one's ancestry, or None when they cannot be counted."""
    procs = process_table()
    if procs is None:
        return None
    mine, p = set(), os.getpid()
    while p in procs and p not in mine and p > 1:
        mine.add(p)
        p = procs[p][0]
    claude = re.compile(r'(^|[/\\])claude(\.exe)?("|\s|$)|@anthropic-ai[/\\]claude-code|claude-code[/\\]cli',
                        re.IGNORECASE)
    return sum(1 for pid, (_, cmd) in procs.items() if pid not in mine and claude.search(cmd))


class Ctx:
    def __init__(self, a):
        self.a = a
        self._busy = False  # not taken yet
        self.busy_why = None
        self.gh = False
        if not a.no_gh and shutil.which("gh"):
            self.gh = run(["gh", "auth", "status"], timeout=20)[0] == 0
        self._cache = {}

    @property
    def busy(self):
        if self._busy is False:
            self._busy, self.busy_why = busy_paths()
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
        return f"cannot tell whether a process uses this folder ({ctx.busy_why})"
    real = norm(path)
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


def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def hidden_changes(path):
    """skip-worktree / assume-unchanged files whose content differs from git.
    Returns the count, or -1 when some file cannot be checked."""
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
    _, fmt = git(path, "rev-parse", "--show-object-format")
    links = [f for f in on_disk if os.path.islink(os.path.join(path, f))]
    files = [f for f in on_disk if f not in links]
    if links and fmt.strip() != "sha1":
        return -1
    disk = {f: blob_sha(os.fsencode(os.readlink(os.path.join(path, f)))) for f in links}
    if files:
        p = subprocess.run(["git", "--no-optional-locks", "-C", path, "hash-object", "--stdin-paths"],
                           input="\n".join(files), capture_output=True, encoding="utf-8",
                           errors="surrogateescape", env=ENV)
        hashes = p.stdout.split()
        if p.returncode != 0 or len(hashes) != len(files):
            return -1
        disk.update(zip(files, hashes))
    return sum(1 for f in on_disk if index.get(f) != disk[f])


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
                rec["unsafe"].append("local tags not checked against the remote (--no-fetch)")
            else:
                remote_tags, failed = set(), False
                for n in servers:  # a fork's tags may live on upstream
                    rc, rt = git(path, "ls-remote", "--tags", n, timeout=120)
                    failed |= rc != 0
                    remote_tags |= {l.split("refs/tags/", 1)[1].replace("^{}", "")
                                    for l in rt.splitlines() if "refs/tags/" in l}
                missing = [t for t in local_tags.split() if t not in remote_tags]
                if missing and failed:
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
    if hidden < 0:
        rec["unsafe"].append("cannot read some skip-worktree or assume-unchanged files: hidden changes unknown")
    elif hidden:
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
        full = os.path.normpath(os.path.join(path, rel))
        k = key(full)
        holds = any(n == k or n.startswith(k + os.sep) for n in nested)
        if code == "!!":
            if not holds and regenerable(rel):
                if rel.endswith("/"):
                    rec["deps"].append(full)
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
    rec = inspect(path, kind, ctx, nested={key(n) for n in inner})
    if parent:
        rec["parent"] = parent
    out = [rec]
    for n in inner:
        sub = inspect_tree(n, "nested", ctx, parent=path)
        out += sub
        rel = posix(os.path.relpath(n, path))
        if sub[0]["verdict"] == "active":
            rec["active"].append(f"nested repo {rel} is active")
        if sub[0]["verdict"] == "keep":
            rec["keep"].append(f"nested repo {rel} is an ongoing project")
        if sub[0]["unsafe"]:
            rec["unsafe"].append(f"nested repo {rel} holds work on no server remote")
        rec["local_only"] += [f"{rel}/{f}" for f in sub[0]["local_only"]]
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
    """On a drive that is not attached now: a macOS volume, a Windows drive letter or share."""
    vol = re.match(r"^(/Volumes/[^/]+)", path)
    if vol:
        return not os.path.ismount(vol.group(1))
    drive = os.path.splitdrive(path)[0]  # "" on macOS and Linux
    return bool(drive) and not os.path.exists(drive + os.sep)


def plugin_items(others):
    base = os.path.join(HOME, ".claude", "plugins")
    known = load_json(os.path.join(base, "known_marketplaces.json"))
    installed = load_json(os.path.join(base, "installed_plugins.json"))
    installed = installed.get("plugins") if isinstance(installed, dict) else None
    readable = isinstance(known, dict) and isinstance(installed, dict)
    installed = installed if readable else {}
    used = [key(e.get("installPath") or "") for v in installed.values() for e in v]
    if others is None:
        later = "cannot count other Claude Code sessions: delete later"
    else:
        later = f"{others} other Claude Code session(s) running: delete later" if others else None
    items = []
    for sub in ("cache", "marketplaces"):
        d = os.path.join(base, sub)
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            full = os.path.join(d, name)
            k = key(full)
            if readable and (name in known or any(u == k or u.startswith(k + os.sep) for u in used)):
                continue
            why = (f"marketplace {name} is not configured and no install uses it" if readable
                   else "cannot read known_marketplaces.json or installed_plugins.json: use unknown")
            items.append({"path": full, "kind": "plugin-cache", "size_mb": size_mb(full),
                          "verdict": "safe" if readable and not later else "active",
                          "notes": [why] + ([later] if later else [])})
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


def configured_roots():
    """The folders listed in ROOTS_FILE (blank lines and # comments skipped), or None."""
    try:
        with open(ROOTS_FILE, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")] or None
    except FileNotFoundError:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", action="append",
                    help=f"folder to scan (repeatable); default: the folders in {ROOTS_FILE}, else ~/dev and ~/.factory")
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
              "gh": bool(ctx.gh), "other_claude_sessions": others, "in_use_check": None, "items": []}

    if a.only:
        p = os.path.realpath(os.path.expanduser(a.only))
        rc, top = git(p, "rev-parse", "--show-toplevel")
        if rc != 0 or norm(top.strip()) != norm(p):
            print(json.dumps({"error": f"{p} is not the top folder of a clone or worktree: "
                                       "re-check its clone instead"}))
            sys.exit(2)
        if not is_linked_worktree(p):
            rec = inspect_tree(p, "clone", ctx)[0]
        else:
            rec = inspect(p, "worktree", ctx)
            wl = worktrees(p)
            rec["main"] = wl[0].get("worktree") if wl else None
            if any("locked" in w for w in wl if w.get("worktree") and norm(w["worktree"]) == norm(p)):
                rec["active"].append("worktree is locked (git worktree lock)")
        ctx._busy = False  # a fresh process snapshot, taken last
        busy = in_use(p, ctx)
        if busy and busy not in rec["active"]:
            rec["active"].append(busy)
        rec["verdict"] = decide(rec)
        report["in_use_check"] = ctx.busy_why or "ok"
        report["items"] = [finish(rec, sizes=False)]
        print(json.dumps(report, indent=1))
        return

    listed = None if a.root else configured_roots()
    report["roots_from"] = "--root" if a.root else ROOTS_FILE if listed else "default: no roots file"
    roots = [os.path.abspath(os.path.expanduser(r)) for r in (a.root or listed or DEFAULT_ROOTS)]
    report["roots"] = [r for r in roots if os.path.isdir(r)]
    report["roots_missing"] = [r for r in roots if not os.path.isdir(r)]
    seen, wts = set(), []
    for r in report["roots"]:
        for repo in find_repos(r, 3):
            if norm(repo) in seen or is_linked_worktree(repo):
                continue
            seen.add(norm(repo))
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
    report["in_use_check"] = (ctx.busy_why or "ok") if ctx._busy is not False else "not needed"
    if not a.no_plugins:
        report["items"] += plugin_items(others)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
