# cozysuite — Claude Code tools

One catalog. Each tool lives in its own repo; this file pins the version.
**Tools install per project, never at user scope**: a repo loads only what its
committed `.claude/settings.json` names.

| Plugin | Solves | Source |
|---|---|---|
| `cozysuite` | `/cozysuite:setup` picks a project's tools: it reads the repo, recommends a preset ± tools, and installs at project scope after you confirm. You start it; it costs no context until then. | `./plugins/cozysuite` |
| `core` (preset) | Any code repo under the workflow: `ai-sdlc`, `ponytail`, `cozy-view`, `cozy-setup`. | `./plugins/core` |
| `site` (preset) | A cozystudio client site: `studio`, `cozyskills`. | `./plugins/site` |
| `cozy-cleanup` | `/cozy-cleanup:cleanup` frees disk space without losing work: it proves what is on GitHub (pushed, or merged as a PR), backs up what git cannot hold, keeps ongoing projects, and deletes only what you approve. Safe while other agents work. You start it. | `./plugins/cozy-cleanup` |
| `ai-sdlc` | Ties agent work to source control and sets up agent orchestration of issues. Needs `mattpocock-skills` (installed with it). | `smcozart/cozy_ai-sdlc` › `plugin/` |
| `ponytail` | Third party: forces the simplest, shortest solution that works. Pinned here, so it moves only when this catalog does. | `DietrichGebert/ponytail` |
| `cozy-setup` | The Cozy Architect output style (select `cozy-setup:Cozy Architect` once) and the macOS PATH guard hook. Private. | `smcozart/cozy_setup` |
| `cozy-view` | Shows how agents and skills really behave: `/cozy-view:trace`, on Claude Code transcripts in Databricks. Sync is off until the machine opts in: `~/.config/cozy_view/profile` holds a Databricks profile name. Private. | `smcozart/cozy_view` |
| `cozyskills` | Report and marketing skills: cozyreport, optimize-seo, optimize-aeo, skill-or-script. Private. | `smcozart/cozyskills` |
| `studio` | cozystudio: client websites from intake to launch, checked the way a visitor sees them, and every later change as a preview and a report. Private. | `smcozart/cozystudio` |

Private: install needs read access to the repo.

## Set up a project

Once per machine: `claude plugin marketplace add anthropics/claude-plugins-official`
(dependencies such as `mattpocock-skills` install from there).

In the project folder, from a shell:

```
claude plugin marketplace add smcozart/cozysuite --scope project
claude plugin marketplace update cozysuite      # the local copy is not refreshed by add
claude plugin install cozysuite@cozysuite --scope project
```

Then in Claude Code: `/reload-plugins`, `/cozysuite:setup`, confirm, `/reload-plugins`,
and commit `.claude/settings.json`. For the workflow files, say "start a new project"
(ai-sdlc `new-project`).

A fresh clone of a set-up repo: run the three commands above (the `add` reads the
repo's own declaration), then `claude plugin install <id> --scope project` for each
`enabledPlugins` entry set to `true`, or open Claude Code and run `/cozysuite:setup`.

A member of an installed preset cannot be disabled on its own. For "a preset minus a
tool", install the remaining members by name instead of the preset.

## Upgrade a project

You choose when; moving a pin here changes no project by itself.

```
claude plugin marketplace update cozysuite
claude plugin update <tool>@cozysuite --scope project
```

Then `/reload-plugins` or restart.

## Release a tool

1. In the tool repo: merge, then `claude plugin tag <dir> --push` (→ `<name>--v<version>`).
2. Here: set its `ref` to the new tag and `sha` to the commit the tag points to
   (`git ls-remote <repo> 'refs/tags/<tag>^{}'`; a lightweight tag has no `^{}` line,
   use the tag's own sha), `claude plugin validate .`, merge. The `sha` pin means a moved
   tag cannot change what installs.

A preset (`plugins/<name>/.claude-plugin/plugin.json`) lists its members in
`dependencies`; bump its `version` only when that list changes. A new tool gets an
entry with a tag and sha; a new preset gets `"category": "preset"`.

## Restore point

`golden-catalog-2026-10-20`: the catalog before the rename (marketplace `smcozart`).
