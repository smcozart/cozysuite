---
name: cleanup
description: Free disk space on this machine without losing work. Finds clones, worktrees, nested repos and plugin caches, proves what is already on GitHub, backs up what git cannot hold, keeps ongoing projects, and deletes only what the developer approves. Safe while other agents work - it skips anything in use, in flight or changed recently. Run it when the laptop holds old clones or worktrees, or after a wave of builds.
disable-model-invocation: true
---

# Clean up this machine

GitHub is the source of truth. A local copy can go when everything in it is on
GitHub or in a backup, and the project is not ongoing. Other agents may work at
the same time: never touch a folder that they use or that is in flight.

The script `scripts/inventory.py` (in this skill's base directory) does the
checks. It only reads; its one write is `git fetch --prune`. Run it with
`python3`. `--help` lists the options.

## 1. Lock and read

1. `mkdir ~/.claude/cleanup.lock`. If that fails, another cleanup is running:
   stop and say so. Remove the lock when you finish or stop, on every path.
2. Run `python3 <base>/scripts/inventory.py`. It scans `~/dev` and
   `~/.factory`. Add `--root <folder>` for each other place the developer
   names. Use the defaults (`--active-hours 24`, `--keep-days 30`) unless the
   developer gives other values.
3. Read the JSON. Each item has a `verdict` and its reasons.

| Verdict | Means | Proposed action |
|---|---|---|
| `active` | A process or agent works in it, a git operation or an open PR is in flight, the worktree is locked, or it changed within `--active-hours`. | None. List it only. |
| `keep` | An ongoing project: a keep marker, use on this machine within `--keep-days`, or a push to GitHub within `--keep-days`. | None. If it also has `unsafe` reasons, offer to push that work (section 3) and nothing else. |
| `unsafe` | Something is on no remote: unpushed commits, stashes, uncommitted or untracked files, linked worktrees, a nested repo with such work. Or the state is unknown: no remote, a failed fetch. | Make it safe (section 3), or leave it. |
| `backup-first` | All git work is on a remote. Files git ignores remain (`local_only`): `.env`, local databases, notes. | Back them up (section 3), then delete. |
| `safe` | Everything is on a remote. A branch that is on no remote but was merged as a PR at that exact tip counts as on GitHub (see `notes`). | Delete. |

Also in the JSON:
- `deps`: build output inside a clone (`node_modules`, `vendor`, ...). For a
  clone that stays, offer to delete only these.
- `plugin-cache`: caches of Claude Code marketplaces that nothing uses.
  Delete only when `other_claude_sessions` is 0, because a running session may
  still read its files.
- `install-record`: plugin records for folders that are gone. They are
  harmless. List them; never edit `~/.claude/plugins/installed_plugins.json`.
  A record on a drive that is not mounted is not stale.

## 2. Show the plan and wait

One table: path, kind, size, verdict, the reasons in a few words, the proposed
action. Then the total space the plan frees. Ask once. The developer approves,
removes or adds items. Run nothing that changes state before they say yes.

- A clone stays unless its verdict is `safe` or `backup-first`. A clone the
  developer names goes only after the same checks pass.
- Worktrees and nested repos inside a clone that stays are their own items.
- Never delete or edit a repo's `.factory/` state (reservations, logs). Its
  orchestrator retires that state. A clone inside `.factory/` is a nested repo,
  with the same checks.
- To keep a project that the checks would remove, mark it as ongoing:
  `git -C <repo> config cleanup.keepUntil <YYYY-MM-DD>` and
  `git -C <repo> config cleanup.keepReason "<why>"`. With no date it stays
  until the marker is removed (`git config --unset cleanup.keepUntil`). The
  marker lives in that clone's `.git/config`: local, never committed.

## 3. Make an approved item safe

- Unpushed branch: `git -C <repo> push origin <branch>`. Never `--force`,
  never `--no-verify`. If the GitHub repo is archived, ask the developer;
  unarchive, push and archive again only on their yes.
- Uncommitted or untracked work: commit it on a new branch
  `backup/cleanup-<YYYY-MM-DD>` and push that branch. If a hook fails, do not
  bypass it: back the files up (next item) and say why.
- Local-only files and anything a hook blocked:
  `(umask 077; tar -czf ~/.claude/backups/cleanup-<YYYY-MM-DD>/<name>.tgz -C <repo> <paths>)`,
  then `tar -tzf` and check the file count. Never push these files.
- A stash: ask the developer. Never drop it.

Then run `inventory.py --only <path>` again. Continue only if it is now `safe`
or `backup-first` with every `local_only` path in a backup.

## 4. Delete, one item at a time

Right before each item, run `inventory.py --only <path>` (add `--active-hours`
or `--keep-days` if the developer gave other values). If the verdict is worse
than in the plan, skip the item and report it. Then:

- Worktree: `git -C <main> worktree remove <path>` (never `--force`; `main` is
  in the JSON). Delete its branch with `git -C <main> branch -d <branch>`. Use
  `-D` only when `notes` say the branch was merged as a PR at that tip. Then
  `git -C <main> worktree prune`.
- Clone or nested repo: `rm -rf '<absolute path>'`, one literal path per
  command.
- Deps: `rm -rf '<absolute path>'` for each approved `deps` path.
- Plugin cache: `rm -rf '<absolute path>'`, only while
  `other_claude_sessions` is 0.

Never delete: a scan root, your home folder, a path outside the scan roots, the
folder you work in, a path written with a variable or a glob. Never delete a
remote branch. Never archive or unarchive a repo without the developer's yes.

## 5. Report

What you deleted and the space freed (`df -h ~` before and after), what you
pushed (repo, branch, sha), each backup (path, file count), and what you
skipped, with the reason. Then remove `~/.claude/cleanup.lock`.
