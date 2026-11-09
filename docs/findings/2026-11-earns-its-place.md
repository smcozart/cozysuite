# Does each tool earn its place? (November 2026 review)

**Tested on:** Windows 11 with Git Bash, on `claude-opus-5-5`, 5 runs per setup with fixed verdict rules and independent audits. **Not yet reviewed on macOS.** Everything here is adopt-or-discard.

**Principle:** a component earns its place by doing one job measurably better than not having it, and it should work in the background without changing how a person develops.

## Results

| Tool | Verdict | Evidence |
|---|---|---|
| **ponytail** | **Keep** | About half the source code (12.6 vs 24.3 lines, p=0.002), median 22 s vs 40 s, hidden tests 25/25 in both setups |
| **ai-sdlc records and conventions** | **Keep** | On real long-running projects: 23/25 with records vs 12/25 without (p=0.001) |
| **ai-sdlc live workflow** | **Lean out** | 1.5–2.9x cost on single sessions and 3.6–4.1x across six, against a 1.25x budget, with no gain when reading records. See the ai-sdlc findings PR |
| **mattpocock-skills code-review** | **Unproven** | Never started on its own (0 of 10). Called by name, it found the same defects as the built-in at 3.6x cost and 2x time, passing 10/10 vs 9/10 |
| **PATH guards** | **Retire or rework** | 0 real catches in 1,365 past tool calls, 14 false alarms, and both missed the 4 real persistent PATH changes |
| **cozy-view** | **Keep** | Not A/B tested. It's the instrument for tracking real use, and it needed a fix to stop counting test runs as real work |
| **cozy-cleanup** | **Keep if useful** | Ported to Windows. Value not tested |

**Fixed cost of the core set:** about 4.9k more context tokens per session, 58 skills in the menu instead of 19, about $0.04 per session.

## Limits

- 5 runs per setup only detects large effects.
- Tasks were controlled and mostly small; most hit a ceiling on correctness, so harder tasks could show differences.
- ponytail was tested on small Python tasks only, part of its speed gain came from command hangs in the runs without it, and it also writes fewer tests of its own.
- code-review's cases were at a ceiling too. Bigger diffs or subtler spec violations might separate it from the built-in.
- Real permission prompts and months of real use were not tested. The next step is real work traced with cozy-view.

## Evidence

Private repo: `smcozart/cozysuite_review_evidence`. It holds the verdict rules (written before the data), hypotheses, audits, per-run results and blind labels. Start with `RESULTS.md`.
