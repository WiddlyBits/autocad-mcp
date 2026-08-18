# autocad-mcp (Gianni's fork)

Third-party open source cloned from `puran-water/autocad-mcp`. Gianni has no relationship with the
author (`hvkshetry`) and cannot request permissions on the upstream repo.

## Tests

```bash
uv run pytest -q
```

`uv` syncs the `dev` dependency group by default, so the bare form works and uses the synced
`.venv`. `uv run --group dev pytest -q` is equivalent and explicit.

Expected counts are branch-dependent — **123 on `main`/`fix-dev-dependency-group`, 134 on
`screenshot-token-cost-controls`, 614 on `visual-cost-ladder`** (439 before the 2026-08-15
space/parity fixes, 578 at `d179a2e`, 586 at `07e0c0c`; the save/open verification work merged
from `claude/distracted-bose-d9f7b6` took it to 614, measured green at `2440c07` on 2026-08-17).
The 11-test gap between the first two is `tests/test_screenshot.py`, which the screenshot branch
extends. A count below the branch's expected number is a real failure; 123 vs 134 on its own is
not.

Re-measure and update this line whenever a commit changes the count — a stale number here turns a
real failure into one that reads as normal.

`tests/golden/*.snap` are committed fixtures, not build output. `python -m tests.generate_golden`
does **not** regenerate them: that takes `--rebaseline-snapshots` *and* `AUTOCAD_MCP_REBASELINE=1`,
two gates, because rebaselining a red snapshot test is the cheapest way to convert a caught
regression into a committed one. Read the diff first.

**Never use `uv run --with pytest --with pytest-asyncio pytest`.** It works, but builds a throwaway
environment on *every* invocation (8 packages reinstalled each time; one session did this 4×).
It was the only option before 2026-08-14 — it isn't any more.

<details>
<summary>Why it used to fail (fixed 2026-08-14)</summary>

`pyproject.toml` declared `dev = ["pytest", "pytest-asyncio"]` as a bare key inside `[project]`.
That is not valid PEP 621 — neither `[project.optional-dependencies]` nor `[dependency-groups]` —
so it was inert and the dev deps never synced. Every obvious invocation failed: `uv run pytest`
("program not found"), `--extra dev` ("not defined in the optional-dependencies table"),
`--group dev` ("not defined in the dependency-groups table"), and
`./.venv/Scripts/python.exe -m pytest` ("No module named pytest" — the venv had neither pytest nor
pip). Fixed by moving those two deps into a proper `[dependency-groups]` table.

This is a local fix not present upstream. It touches `pyproject.toml` and `uv.lock`, so keep it on
its own commit — don't fold it into a feature branch staged for an upstream PR.
</details>

## Git

```
origin    https://github.com/WiddlyBits/autocad-mcp.git   <- Gianni's fork, push here
upstream  https://github.com/puran-water/autocad-mcp.git  <- author's repo, pull only
```

- Local `main` tracks `upstream/main`; feature branches track `origin`. Pushing to `upstream`
  always 403s — the credential authenticates as WiddlyBits, which has no write bit there.
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

## Screenshot cost controls

`view(operation="get_screenshot")` takes `max_dimension` (longest side px, default 1280, clamped
64–2576 via `SCREENSHOT_MAX_DIMENSION` in `config.py`). Implemented in `screenshot.py`
(`_resize_and_encode`, PIL), threaded through `base.py` / `file_ipc.py` / `ezdxf_backend.py` /
`client.py` / `server.py`. Payload shape is `{"data":..., "mime":...}`, not a bare base64 string.

`quality` (JPEG) shrinks bytes on the wire but **does not** reduce token cost — cost is
`ceil(w/28) × ceil(h/28)`, a function of dimensions only. Nothing above 2576 px helps.
