# autocad-mcp (Gianni's fork)

Third-party open source cloned from `puran-water/autocad-mcp`. Gianni has no relationship with the
author (`hvkshetry`) and cannot request permissions on the upstream repo.

Before running more than 2-3 find/ls sweeps hunting for a source file, ask Gianni for the
path instead — Dropbox layouts change and blind search costs 10x a question.

## Tests

```bash
uv run pytest -q
```

Expected count on `main`: **761**. A lower count is a real failure. Update this number in the
same commit that changes it — a stale number turns a real failure into one that reads as
normal. Feature branches are measured when they are made, not recorded here.

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
- **`gh` CLI is installed** (`C:\Program Files\GitHub CLI`, on Git Bash PATH) and authenticated as
  WiddlyBits via keyring. Use it for CI: `gh run list -R WiddlyBits/autocad-mcp`,
  `gh run view <id> -R WiddlyBits/autocad-mcp --log-failed`. CI runs on `ubuntu-latest`, so a
  test that only passes because `C:/temp` exists locally will fail there.
- For PRs to upstream, build the compare URL by hand and let Gianni decide whether to submit:
  `https://github.com/puran-water/autocad-mcp/compare/main...WiddlyBits:autocad-mcp:<branch>?expand=1`
- Identity is set **repo-local only** to `Gianni <giannimagnabooking@gmail.com>` — deliberately not
  global. Never commit as `hvkshetry`.

## Skills

`skills/` is the **only** source of truth for the AutoCAD skills — `autocad-mcp-workflow`,
`autocad-save-verification`, `title-block-text`. Every other copy is a build artifact. Edit
here, never in a loaded copy: editing a loaded copy is how a skill once documented a
`preflight` block and an `mcp_select.lsp` that existed only on an unmerged branch.

`skills/** text eol=lf` (`.gitattributes`), so the loaded copies are byte-identical to the
repo and a plain recursive diff is a valid drift check.

```
powershell -File scripts\sync-skills.ps1           # copy to ~/.claude/skills/synced/<guid>_<guid>/ (local preview)
powershell -File scripts\sync-skills.ps1 -Check    # diff every loaded copy + manifest updatedAt; exit 1 on drift
powershell -File scripts\sync-skills.ps1 -Package  # dist\<skill>.zip for the claude.ai upload
```

Run `-Check` at the start of any AutoCAD session that edits skills. The synced folder is a
**local preview**: the next claude.ai sync round overwrites it, so it is not delivery. Delivery
is Gianni uploading the `-Package` zips in claude.ai → Customize → Skills after a merge to
`main`; `-Check` afterwards must list all three in the manifest with `updatedAt` at or after
the last commit touching `skills/`.

`tests/test_skill_drift.py` fails CI when a skill names an `mcp:`/`c:` LISP function or a
`tool(operation=…)` that does not exist in `src/` or `lisp-code/`.

Error payloads and what to do about them: `skills/autocad-mcp-workflow/references/troubleshooting.md`.
Developer detail: `docs/error-model.md`. Open defects: `docs/known-issues.md`.

## Screenshot cost controls

`view(operation="get_screenshot")` takes `max_dimension` (longest side px, default 1280, clamped
64–2576 via `SCREENSHOT_MAX_DIMENSION` in `config.py`). Implemented in `screenshot.py`
(`_resize_and_encode`, PIL), threaded through `base.py` / `file_ipc.py` / `ezdxf_backend.py` /
`client.py` / `server.py`. Payload shape is `{"data":..., "mime":...}`, not a bare base64 string.

Cost guidance for the agent (the token formula, `region`, `save_to`) lives in Rule 3 of the
workflow skill, not here.
