# Why this skill exists — the measurements

Read this when you want the evidence behind a rule, or when you are tempted to relax one.
`SKILL.md` carries the rules; this file carries the numbers, so that the rules stay short
enough to actually be followed.

All figures from `tests/token_audit.py` in `~/autocad-mcp`, which is offline and free:

```bash
uv run python -m tests.token_audit ~/.claude/projects/C--Users-Gianni-autocad-mcp/<session>.jsonl
```

## 2026-08-11 — the session that started it

An 8-hour ECSI panel rebuild turned **40 user turns into 314 API requests and 147.5 M
cache-read tokens** — about 3.7 M of context re-read for every single thing Gianni asked
for — and hit forced context-compaction three times.

Measured on 2026-08-14, images were **18.9%** of that: 27.9 M resident tokens of 147.5 M.
The other ~81% was ordinary conversation, thinking, and tool text accumulating across 314
requests at 482 K average context. **The often-quoted "images were 99.7%" figure is bytes,
not tokens.**

So the first lever was session length and request count; images were second.

The same measurement showed something about behaviour worth keeping in mind: **0 of 30
screenshots** were taken without a cheap probe in the preceding 3 calls. The model tried
the cheap query first every single time. It fell back to pixels because the cheap calls
could not answer — which has since been fixed by giving `drawing(info)` real extents and
`entity(get)` real geometry and text. Don't treat screenshot use as a discipline failure.
Climb the ladder, and if a rung genuinely can't answer, taking the picture is correct.

## 2026-08-22 — the ladder worked, and the cost moved

Five sessions of selection-driven editing: "lengthen the lines I have selected", "move
these circles to their line endpoints", "duplicate these 16 labels into DI-03".

| Session | Requests | User turns | Cache-read | Avg context |
|---|---|---|---|---|
| `dbf5fe23` | 73 | 10 | 8.5 M | 119.3 K |
| `bb47df42` | 46 | 7 | 5.0 M | ~110 K |
| `a184402f` | 50 | 9 | 5.1 M | 105.4 K |
| `1f194232` | 41 | 9 | 4.0 M | 100.4 K |
| **total** | **210** | **35** | **22.6 M** | |

646 K cache-read per user turn — **5.7× better than 2026-08-11**. Six screenshots all day,
1.2% of the bill. The capture ladder is working and does not need tightening.

What replaced it: **6.0 requests per user turn**, and **129 of 193 tool calls were
`execute_lisp`**, each one a request at ~108 K cache-read. That is where the money went,
and it is why `SKILL.md` now leads with round-trip count rather than pixels.

### The stumbles that produced `mcp_select.lsp`

| What happened | Evidence |
|---|---|
| `(ssget "_I")` returned NONE on 4 of 4 attempts; `(ssgetfirst)` returned `(-1 -1)`. Not a sysvar problem — `PICKFIRST=1 PICKADD=2 CMDACTIVE=0`. Typing the dispatch trigger clears the implied set. | `bb47df42` 19:43, 19:47, 22:11; `a184402f` 18:24 |
| No documented handoff, so it got rediscovered live: 5 `AskUserQuestion` turns, and Gianni had to ask *"how do I run the select previous command you mentioned?"* | `dbf5fe23` 17:23–17:54 |
| Characterising one selection took 4 round-trips — count, types, coordinates, lengths — with the same `(while (< i n) … strcat)` retyped each time. | `dbf5fe23` 16:51–16:52, 17:25–17:26 |
| A bare `ssget "_X"` swept paper space into a model-space move: everything moved −9.565795, moved back +9.565795, then redone model-only at −9.56971715. Three mutations for one edit, reversed by an inverse move with a **different number**. | `a184402f` 18:27 → 18:29 → 18:32 |
| `PASTECLIP` landed in paper space because `CTAB="11x17"`. 16 objects pasted, `entdel`'d, `CTAB` set to Model, re-pasted. | `bb47df42` 21:13 → 21:14 → 21:15 |
| `vla-getboundingbox 'mn 'mx` collided with locals named `mn`/`mx` → *"bad argument type for compare: 1 #<safearray…>"*. | `a184402f` 18:29 |
| `vla-get-Width` on a TEXT — an MTEXT-only property → *"unknown name: Width"*. | `bb47df42` 19:49 |
| `wcmatch` pattern written from memory: `"Bank #, Point #"` matched **0 of 152**; a debug dump; then `"Bank #*Point #"` matched 16. There is a non-obvious character after the comma. | `dbf5fe23` 17:57 → 18:02 |
| An edit ran on an un-echoed `ssget "_P"` set and left strays `98A`/`98B` on layer `TITLE`. Finding and deleting them cost 3 undos, ~10 probes, 3 screenshots and 2 zoom guesses. | `dbf5fe23` 17:26–17:48 |
| `sssetfirst` highlighted those two strays correctly (`selcount=2`) — and Gianni replied *"I don't see it selected"*, because they were outside the current view. An echo nobody can see confirms nothing. | `dbf5fe23` 17:39 |
| Four separate "done, DI-0x is focused now" turns in ten minutes, plus an `AskUserQuestion` with one option → `InputValidationError`. | `a184402f` 18:25–18:32 |
| The `.scr` batching advice already existed — in a reference file. 129 individual `execute_lisp` calls say nobody read it. **Rules that change behaviour have to live in `SKILL.md`.** | all sessions |

### One correction to the record

That day's check for whether the probe library was loaded was
`(member 'mcp:text-dump-in (atoms-family 1))`, which answered `NOT LOADED`. **That check
is broken**: `atoms-family` with format 1 returns a list of *strings*, so comparing a
quoted symbol against it is nil whether or not the function exists. The probes were
probably genuinely absent — the APPLOAD setup steps only ever added `mcp_dispatch.lsp` —
but that particular answer was not evidence of it.

The preflight now uses `(type mcp:sel-dump)`. An unbound symbol evaluates to nil in
AutoLISP rather than erroring, so it is both safe and actually discriminating.
