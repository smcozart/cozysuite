---
name: earns-its-place
description: Judge an agentic tool (plugin, skill, harness, app) against the cozyframe framework. Is its goal defined, does every part earn its place, and what is the smallest change that keeps its purpose and character? Read-only; it recommends and never edits.
disable-model-invocation: true
---

# Earns its place

The **target** is the folder, repo, tag or plugin the developer names. The **framework** is `../../FRAMEWORK.md`; read it first and apply every principle in it. This run reads only. It writes one report file and changes nothing else: no edits, commits, issues, comments or installs.

## 1. Goal

Find the target's stated primary goal and non-goals: its README, `SYSTEM-INTENT.md`, plugin description, or `AGENTS.md`. Write the goal as one sentence, in the target's own words where they exist.

If the goal is missing, vague, or two goals compete, draft one sentence and up to three non-goals, show them, and ask the developer to confirm or edit before going on.

**Done when:** the developer has confirmed one goal sentence.

## 2. Character

List the 3 to 5 non-negotiables that make the target itself. For example: "a human merges gate changes", or "the developer never sees a template". Take them from the target's own docs and decisions, and ask the developer to confirm them.

**Done when:** a confirmed character list exists. Every later recommendation keeps all of it.

## 3. Inventory

List every part: each skill, rule, gate check, hook, doc, template file and script. Give each one its cost, measured, not estimated: words, lines, runtime (run it, time it), and how often an agent must read it.

**Done when:** every file in the target is in a part or named as outside the inventory, and every cost is a number.

## 4. Evidence

For each part, find evidence of real use toward the goal. Use what the target's history holds:
- real FAILs in CI;
- calls in session transcripts;
- defects a review caught;
- issue and PR threads.

Self-tests prove that a part works, not that it helps (framework principle 3), so they are not evidence of use.

Give each part one verdict:
- **keep:** evidence of use toward the goal;
- **simplify:** used, but costs more than its use needs;
- **cut:** no use, or it serves no goal;
- **unproven:** cannot tell yet; name the dated test that would tell.

**Done when:** every part has a verdict and its evidence (file:line, run id, transcript id or issue), or the words "no evidence found" with what you searched.

## 5. Recommendations and A/B plan

Rank at most 5 changes, most value first. Each one has:
- what changes;
- the evidence;
- the cost of the change;
- which character items it touches (none is the target);
- its **earned-its-place test:** a measurable result on real work, and the date or count at which it is judged.

Then write the A/B plan:
- the before tag;
- where the after branch is built;
- the real-use tasks, run in a repo the target did not come from;
- the decision rule, fixed before the run;
- the landing point at which this round stops.

Saying "this tool is lean; change nothing" is a valid result when the evidence supports it.

**Done when:** the report is written to the path the developer names (default `cozyframe-report-<target>-<YYYY-MM-DD>.md` in the current folder). It holds the confirmed goal, the confirmed character, the inventory with costs and verdicts, at most 5 recommendations, and the A/B plan.
