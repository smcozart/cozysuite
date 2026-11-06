---
name: cleanup
description: Free disk space on this machine without losing work. Finds clones, worktrees, nested repos and plugin caches, proves what is already on GitHub, backs up what git cannot hold, keeps ongoing projects, and deletes only what the developer approves. Safe while other agents work - it skips anything in use, in flight or changed recently. Run it when the laptop holds old clones or worktrees, or after a wave of builds.
disable-model-invocation: true
---

# Clean up this machine

GitHub is the source of truth. A local copy can go when everything in it is on a
server remote or in a backup, and the project is not ongoing. Other agents may
work at the same time: never touch a folder that they use or that is in flight.

The script `scripts/inventory.py` (in this skill's base directory) does the
checks. It only reads; its one write is `git fetch --prune` on clones that are
not active. Run it with `python3` (nothing to install). `--help`
lists the options. The commands below work in a POSIX shell: macOS, Linux, and
Git Bash on Windows.

## 1. Lock and read

1. `mkdir ~/.claude/cleanup.lock && echo "session ${CLAUDE_CODE_SESSION_ID:-unknown} in $PWD, $(date)" > ~/.claude/cleanup.lock/owner`.
   If `mkdir` fails, another cleanup runs or one crashed: show the `owner`
   file and stop. Only the developer removes a lock. Remove your own lock
   when you finish or stop, on every path.
2. Run `python3 <base>/scripts/inventory.py`. With no `--root` it scans the
   folders listed in `~/.config/cozy-cleanup/roots` (one per line, `#` for
   comments), else `~/dev` and `~/.factory`. If `roots_from` in the JSON says
   `default: no roots file`, ask the developer once which folders hold their
   code (on Windows often another drive: write `D:/dev`, not `/d/dev`), write them to that
   file (`mkdir -p ~/.config/cozy-cleanup` first), and run again.
   `roots_missing` lists roots that are not there (a drive not attached): say
   so. `--root` replaces the roots for one run: pass only the places the
   developer names. Keep the defaults (`--active-hours 24`, `--keep-days 30`) unless the
   developer gives other values. Never lower them to get an item through.
   Never pass `--no-gh` or `--no-fetch`: they turn checks off and are for
   tests only. If `gh` is `false` in the JSON, most items come out `keep` or
   `active`: ask the developer to run `gh auth login`, then run again. If
   `in_use_check` is not `ok`, no process check ran and every clone comes out
   `active`: tell the developer its reason (on macOS or Linux, install `lsof`).
3. Read the JSON. Each item has a `verdict` and its reasons.

| Verdict | Means | Proposed action |
|---|---|---|
| `active` | A process or agent has files or its working folder in it, a git operation or an open PR is in flight, the worktree is locked, it changed within `--active-hours`, or one of these cannot be checked. | None. List it only. |
| `keep` | An ongoing project: a keep marker, use within `--keep-days`, a GitHub push within `--keep-days`, `.factory/` orchestrator state, or the last push cannot be checked. | None. If it also has `unsafe` reasons, offer to push that work (section 3) and nothing else. |
| `unsafe` | Something is on no server remote: unpushed commits (on branches, tags or notes), tags or LFS objects not on the remote, stashes, uncommitted, hidden or untracked changes, linked worktrees, a nested repo with such work. Or the state is unknown. A remote that is a folder on this disk does not count. | Make it safe (section 3), or leave it. |
| `backup-first` | All git work is on a server remote. Files git ignores remain (`local_only`): `.env`, local databases, notes. | Back them up (section 3), then delete. |
| `safe` | Everything is on a server remote. A branch on no remote that was merged as a PR at that exact tip counts (see `notes`). | Delete. |

Also in the JSON:
- `deps`: build output inside a clone (`node_modules`, `vendor`, ...). Offer to
  delete only these for a clone that stays, and only when that clone has no
  `active` reasons.
- `plugin-cache`: caches of Claude Code marketplaces that nothing uses. Delete
  only while `other_claude_sessions` is 0: a running session may still read
  them. `null` means the sessions could not be counted: not 0.
- `install-record`: plugin records for folders that are gone. They are
  harmless. List them; never edit `~/.claude/plugins/installed_plugins.json`.
- `size_mb` of a clone includes its nested repos. Do not add both to a total.

## 2. Show the plan and wait

One table: path, kind, size, verdict, the reasons in a few words, the proposed
action. Then the total space the plan frees. Ask once. The developer approves,
removes or adds items. Run nothing that changes state before they say yes.

- A clone goes only if its verdict is `safe` or `backup-first`, also when the
  developer names it.
- Worktrees and nested repos inside a clone that stays are their own items.
- Never delete or edit a repo's `.factory/` state (reservations, logs). Its
  orchestrator retires that state. A clone that holds it is always `keep`.
- To keep a project that the checks would remove, mark it as ongoing:
  `git -C <repo> config cleanup.keepUntil <YYYY-MM-DD>` and
  `git -C <repo> config cleanup.keepReason "<why>"`. With only a reason (or
  `cleanup.keep true`), it stays until the marker is removed. The marker is in
  that clone's `.git/config`: local, never committed.

## 3. Make an approved item safe

Backups go to `~/.claude/backups/cleanup-<YYYY-MM-DD>/`. Make the folder with
`(umask 077; mkdir -p <folder>)` (on Windows it takes the private permissions of
the home folder instead). Write the backup folder starting with `~/`, never as
`C:/...`: GNU tar, as in Git Bash, reads `C:` in a `-f` path as a remote host.
The `-C <repo>` path may be the JSON's path as it is.

- Unpushed commits on a branch: `git -C <repo> push origin <branch>`. A tag:
  `git -C <repo> push origin <tag>`. Never `--force`, never `--no-verify`. If
  the GitHub repo is archived, ask the developer; unarchive, push and archive
  again only on their yes.
- Untracked or uncommitted files: back them up, do not commit them by
  default. One tarball per item with every `untracked` and `local_only` path:
  `(umask 077; tar -czf <folder>/<name>.tgz -C <repo> <paths>)`; for
  uncommitted changes to tracked files also
  `git -C <repo> diff HEAD > <folder>/<name>.patch`. Hidden changes
  (skip-worktree, assume-unchanged): ask the developer.
  Push them to a `backup/cleanup-<YYYY-MM-DD>` branch instead only when all
  three hold: the developer names the files, `visibility` is `PRIVATE`, and
  `grep -nE '(sk-|ghp_|github_pat_|xox[bpa]-|AKIA|dapi)[A-Za-z0-9_-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY'`
  finds nothing in them. If a hook fails, do not bypass it: back them up.
- Local-only files: the same `tar`, with every `local_only` path. Never push
  them.
- A stash: ask the developer. Never drop it.
- After each `tar`, run `tar -tzf` and check that every path is in it.

An item that you pushed or committed in this step now looks recent (a push
also makes its GitHub repo look in use): do not delete it in this run. Tell
the developer it goes in a later run. An item that you only backed up with
`tar` can continue.

## 4. Delete, one item at a time

Right before each item, run `inventory.py --only <path>` with the same options
(for `deps`, `--only` the clone that holds them; it must have no `active`
reasons). Continue only if the re-check verdict is one of:
- `safe`;
- `backup-first`, and every `local_only` path is in the backup;
- `unsafe`, and its only reasons are untracked paths or uncommitted changes
  to tracked files, every `untracked` and `local_only` path is in the backup,
  and the `.patch` exists when there are uncommitted changes.

"In the backup" means `tar -tzf` lists the path (for a folder, its contents).
A path that is not in it: back it up first. Anything else, or a verdict worse
than in the plan: skip the item and report it. Then:

- Worktree: `git -C <main> worktree remove <path>` (never `--force`; `main` is
  in the JSON). Delete its branch with `git -C <main> branch -d <branch>`. Use
  `-D` only when `notes` say the branch was merged as a PR at that tip. Then
  `git -C <main> worktree prune`.
- Clone or nested repo: `rm -rf '<absolute path>'`, one literal path per
  command. On Windows a file that a process holds open cannot be deleted: if
  `rm` reports one, stop on that item and report it as `active`.
- Deps: `rm -rf '<absolute path>'` for each approved `deps` path.
- Plugin cache: `rm -rf '<absolute path>'`, only while `other_claude_sessions`
  is 0.

Never delete: a scan root, your home folder, a path outside the scan roots
(except `plugin-cache` items under `~/.claude/plugins`), the folder you work
in, a path written with a variable or a glob. Never delete a remote branch.
Never archive or unarchive a repo without the developer's yes.

The in-use check sees open files, open folders and working folders (`lsof` on
macOS and Linux; on Windows the system's handle table). Like `lsof` without
`sudo`, it does not see processes of other users or ones run as administrator. An agent that only runs
short commands in a folder (`git -C`) shows up as recent activity instead:
that is why `--active-hours` stays at 24 or more while agents run. Activity
comes from commits, the index, `HEAD` and changed files. A fetch or a
ref-only change does not count, but any work it makes (a new commit, branch,
tag or worktree) still fails the content checks.

## 5. Report

What you deleted and the space freed (`df -h <each root> ~` before and after:
one line per drive or volume), what you
pushed (repo, branch or tag, sha), each backup (path, file count), and what you
skipped, with the reason. Then remove `~/.claude/cleanup.lock`.
