# Why this skill exists

Read this when you are tempted to relax a rule. `SKILL.md` carries the rules; this file
carries the reasons. The session-cost numbers live in the global CLAUDE.md; re-measure any
transcript offline with

```bash
uv run python -m tests.token_audit ~/.claude/projects/C--Users-Gianni/<session>.jsonl
```

**Session length and request count are the cost, images second.** The 2026-08-11 panel
rebuild was dominated by ordinary conversation and tool text re-read on every request;
images were under a fifth of it. The often-quoted "images were 99.7%" is bytes, not tokens.

**The model already probes before it screenshots.** In that session every screenshot
followed a cheap probe that could not answer. The fix was giving the cheap rungs real
answers (`drawing(info)` extents, `entity(get)` geometry and text) — not scolding. If a
rung genuinely can't answer, the picture is correct.

**Once the ladder worked, the cost moved to `execute_lisp` round trips** (2026-08-22: about
two thirds of all tool calls). That is why `SKILL.md` leads with round-trip count and why
batching to `.scr` is a rule in `SKILL.md`, not a tip in a reference — it was already in a
reference file and nobody read it.

**`mcp_select.lsp` exists because of 2026-08-22's stumbles:** `(ssget "_I")` was nil every
time (the dispatch trigger clears the implied set); the handoff was rediscovered live over
five questions; one selection took four round trips to characterise; an un-echoed
`ssget "_P"` edit left strays on `TITLE`; and a highlight outside the view confirmed nothing.
Each became a rule: HS handoff, one `sel-dump`, `sel-show` echo, handle lists, the guard.
