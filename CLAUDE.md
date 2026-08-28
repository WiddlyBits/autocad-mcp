# autocad-mcp (Gianni's fork)

Third-party open source cloned from `puran-water/autocad-mcp`. Gianni has no relationship with the
author (`hvkshetry`) and cannot request permissions on the upstream repo.

## Tests

```bash
uv run pytest -q
```

Expected counts are branch-dependent — **123 on `fix-dev-dependency-group`, 134 on
`screenshot-token-cost-controls`, 614 on `visual-cost-ladder`, 690 on
`selection-driven-editing`, 715 on `main`**. A count below the branch's expected number is a
real failure. Re-measure and update this line whenever a commit changes the count — a stale
number here turns a real failure into one that reads as normal.

`tests/golden/*.snap` are committed fixtures, not build output. Regenerating them requires both
`--rebaseline-snapshots` and `AUTOCAD_MCP_REBASELINE=1` — two gates, because rebaselining a red
snapshot is the cheapest way to convert a caught regression into a committed one. Read the diff first.

## Git

```
origin    https://github.com/WiddlyBits/autocad-mcp.git   <- Gianni's fork, push here
upstream  https://github.com/puran-water/autocad-mcp.git  <- author's repo, pull only
```

- **`main` and every feature branch track `origin`.** `main` tracked `upstream/main` until
  2026-08-18; it was retargeted so a bare `git push` from `main` reaches the fork instead of
  403ing against `upstream`. Pushing to `upstream` still always 403s — the credential
  authenticates as WiddlyBits, which has no write bit there.
- **A bare `git pull` on `main` now takes from the fork, not the author.** To take upstream's
  changes, name it: `git pull upstream main`. `main` is 26 commits ahead of `upstream/main`
  as of 2026-08-18, so that merge is a real event, not a fast-forward.
- **A github.com credential persists in Git Credential Manager** (Gianni signed in 2026-08-13).
  Pushes complete unattended with no dialog — verified 2026-08-14 and 2026-08-15. Foreground is
  fine in this regime; backgrounding is only needed if the credential lapses.
- **Tell which regime you are in before pushing, without popping a dialog:**
  `GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=echo git push --dry-run origin <branch>`. Exit 0 means a
  credential is stored and the real push goes through unattended. `could not read Username for
  'https://github.com'` means the store is empty — a foreground push then **hangs until the tool
  timeout**, so run it with `run_in_background: true` and GCM's dialog reaches Gianni's desktop.
  (`cmdkey /list` is blocked by the auto-mode classifier; use the dry-run.)
- **Verify every push independently** with `git ls-remote --heads origin <branch>`, comparing the
  SHA to local `HEAD`. Exit 0 alone is not proof.
- Claude never supplies the credential; Gianni authenticates in GCM's own UI.
- **No `gh` CLI installed.** Build the compare URL by hand and let Gianni decide whether to submit:
  `https://github.com/puran-water/autocad-mcp/compare/main...WiddlyBits:autocad-mcp:<branch>?expand=1`
- Identity is set **repo-local only** to `Gianni <giannimagnabooking@gmail.com>` — deliberately not
  global. Never commit as `hvkshetry`.

## Skills

`skills/` is the canonical copy of the AutoCAD skills — `autocad-mcp-workflow` and
`autocad-save-verification` — versioned next to the server they document. Deploy them with

```
powershell -File scripts/sync-skills.ps1        # -WhatIf to preview
```

which globs the session GUIDs under
`%APPDATA%\Claude\local-agent-mode-sessions\skills-plugin\*\*\skills` rather than
hardcoding them, because those are exactly what the app re-provisions.

**Edit here, not there.** The AppData copy is a deployment target. Editing it in place is
how the skill came to document a `preflight` block and an `mcp_select.lsp` that existed
only on an unmerged branch — a divergence nothing could catch while one half was outside
version control. That gap closed when `selection-driven-editing` landed on 2026-08-25.

### Skill selection

| Trigger | Load this skill |
|---|---|
| Any `mcp__autocad-mcp__*` call; editing a .dwg; selection handoff; LISP batching; DXF probe; screenshot planning; troubleshooting the MCP connection | `autocad-mcp-workflow` |
| Saving/saving-as a .dwg; DXFOUT; `drawing(save_as_dxf)`; opening/closing a drawing; any write that must be verified | `autocad-save-verification` |

### LISP / probe error contracts

Exact payload signatures — match these without re-reading the LSP files:

- **`WRONG-DOC`** — `{"ok":false,"error":"WRONG-DOC","wanted_dwg":…,"wanted_tab":…,"actual_dwg":…,"actual_tab":…}`. Use `actual_dwg`/`actual_tab` directly; no round-trip re-probe needed.
- **`UNRESOLVED-REF`** — `{"ok":false,"error":"UNRESOLVED-REF","ref":…}`. The reference coordinate was not found in the selection set; check `(mcp:sel-dump)` coordinates before retrying.
- **Probe truncation** — `"truncated":true,"truncated_reason":"max_entities"|"time"`. Fix: switch to the `-in` form with a specific layer or tighter region.
- **sel-dump truncation** — `"truncated":true` in the sel-dump payload (count > `*mcp-max-selection*` = 200). Re-select a tighter set.

## Screenshot cost controls

`view(operation="get_screenshot")` takes `max_dimension` (longest side px, default 1280, clamped
64–2576 via `SCREENSHOT_MAX_DIMENSION` in `config.py`). Implemented in `screenshot.py`
(`_resize_and_encode`, PIL), threaded through `base.py` / `file_ipc.py` / `ezdxf_backend.py` /
`client.py` / `server.py`. Payload shape is `{"data":..., "mime":...}`, not a bare base64 string.

`quality` (JPEG) shrinks bytes on the wire but **does not** reduce token cost — cost is
`ceil(w/28) × ceil(h/28)`, a function of dimensions only. Nothing above 2576 px helps.
