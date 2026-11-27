# cozyframework

**Status:** confirmed by the owner on 2026-11-26 ("framework confirmed"); inferred from the owner's recorded decisions (smcozart/cozy_ai-sdlc#121 and its threads). Version: 0.2 (principles 8 and 9 added on 2026-11-27, owner: "add v0.2"; both came from the v0 tests).

Each principle is a test the evaluator can apply. "Source" quotes the owner's own words.

1. **Earns its place.** Every part (skill, rule, check, doc, file, step) shows evidence of use toward the tool's goal: a real call, a real FAIL, a defect it caught. A part with no evidence is *unproven*: it gets a dated test or it goes. *Source:* "every component earns its place and don't over engineer".
2. **Landing point first.** Every change states when it is done and when iteration stops, before work starts. *Source:* "no iterating indefinitely... define a landing point".
3. **Real use decides.** A tool never certifies itself. A change is accepted on real work in a repo the change did not come from. A tool's own gate, witness or self-evaluation proves that a check works, not that the tool helps. *Source:* "I need to not have the tool test against itself, but I need real use cases".
4. **Baseline before change.** Tag the before state, fix the decision rule before the run, and compare before and after on the same measure. *Source:* "tag the current version on main as our golden so we can easily revert and then compare the before vs after".
5. **Keep the character.** A simplification keeps the tool's purpose and its confirmed non-negotiables, so the tool stays recognizably itself. *Source:* "simplifying the tool while maintaining its function and character".
6. **Humans own the irreversible.** Merges of the gate itself, deletes, and identity or permission changes wait for a named human. *Source:* the held-merge checkoffs; declining the delete-repo permission; "rm rf is very risky".
7. **Fresh eyes review.** A reviewer who did not write the work, and who is given the work rather than the author's conclusion, reads it before it counts. *Source:* "have another herdr session with fable 5.2 review both the change and PR 183".
8. **Guards are judged by the loss they prevent.** A part that guards against an irreversible loss (lost work, a deleted repo, a bad merge) earns its place by the loss it would prevent, not by how often it fires. It needs a proof that it works (one fail-first case), not a record of use. *Source:* the v0 false-positive check on cozy-cleanup kept its unexercised backup steps by judgement; this makes the judgement explicit.
9. **Redundant parts go.** A part is redundant when another required part already catches the same failure. Name the part that covers it, then cut the redundant one, even if it once fired. *Source:* `dispatch-hold` was cut on cozy_ai-sdlc#181 because the adversary reads every skill diff; the v0 back-test kept it without weighing that.
