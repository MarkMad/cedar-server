# Repository Guidelines

## Project Structure & Module Organization

`cedar/` contains the Python FastAPI backend. `main.py` assembles the app;
`routes/` groups endpoints by feature. Keep persistence in `db.py`, environment
configuration in `config.py`, document extraction in `chunker.py`, and Kokoro
synthesis, alignment, and caching in `tts.py`.

`tests/` holds pytest coverage. `tools/` contains language/alignment self-tests
and Gutenberg corpus builders. `docs/img/` stores README images. `data/` is
ignored runtime storage for SQLite, uploads, logs, and audio. Docker deployment
uses the root Dockerfile, entrypoint, and Compose files.

## Build, Test, and Development Commands

Use Python 3.12, matching CI. Create and activate a virtual environment, then run:

```bash
pip install -r requirements-dev.txt
uvicorn cedar.main:app --host 0.0.0.0 --port 8000
ruff check cedar tools tests
pytest tests -q
python tools/lang_selftest.py
python tools/align_selftest.py
docker compose up -d --build
```

Uvicorn runs the API locally; it needs a reachable Kokoro service configured
through `CEDAR_KOKORO_URL`. Compose builds the API and starts Kokoro together.
The lint, pytest, and self-test commands mirror CI validation. `lame` is optional
but recommended for compact audio with accurate word alignment.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules, `PascalCase` classes,
and uppercase constants. Add type annotations to new interfaces and concise
docstrings for non-obvious behavior. Follow nearby code and `ruff.toml`, which
sets a 110-character line length and selected error checks. Keep blocking I/O
and CPU work off async request paths; preserve bounded synthesis concurrency.

## Testing Guidelines

Name files `test_*.py` and functions `test_<behavior>`. Fixtures in `conftest.py`
use temporary storage and stub Kokoro; tests should not require live services.
Add regression tests for changed behavior, especially transactions, concurrent
updates, authentication, and timestamp alignment. No numeric coverage threshold
is configured. Run relevant tests during development and all CI checks before
submitting code changes.

## Commit & Pull Request Guidelines

Recent commits use descriptive imperative subjects, such as “Limit CPU speech
synthesis concurrency and preserve shared jobs”; no enforced prefix convention
is evident. Keep commits focused. PR descriptions should explain the problem,
resulting behavior, validation, and configuration or compatibility impacts.
Link related issues when available; include screenshots for visible UI changes.

## Security & Configuration Tips

Use `.env.example` as the configuration reference. Never commit `.env`, owner
keys, or runtime data. Preserve the deny-by-default owner-key gate, SSRF-safe
fetching, and request-size limits. Back up SQLite before applying migrations or
rechunking existing documents, and preserve annotation anchors and audio caches.

## Change authorization

Only make the edits the request covers. Everything else needs permission first.

- A question is not authorization to change code. "Why is this error happening?"
  is answered with an explanation; the fix is made when the user asks for it.
- Before editing a file the request did not cover, stop and ask. Say what you
  would change, in which files, and why, then wait for an answer.
- In scope without further asking: the requested change, the files it needs,
  and a regression test for the behavior it changes.
- Out of scope without asking: unrelated failing tests, other modules, refactors
  near the change, formatting, and dependency or configuration edits. Report
  them instead — a separate failure found while working is not a licence to
  start fixing it.
- Investigation needs no permission. Reading files, searching, running tests
  and read-only diagnostics are always fine; only writes need approval.
- Work already done that the request did not cover must be reported plainly,
  with the files changed, and offered for revert — not folded into the next
  commit and not quietly left in the tree.

This applies to delegated work too: a sub-agent brief authorizes only what the
brief names.

## Delegate tasks

The main agent orchestrates delegated work and remains responsible for
integration, verification, and the final result. Delegate by choosing the right
*sub-agent kind* for the task; do not treat it as an automatic model-escalation
ladder. Two sub-agents are available via the `subagent` tool:

- `explore` — fast, read-only codebase exploration: find files by pattern,
  search for keywords/symbols, and answer "how does X work" questions. State the
  desired thoroughness (`quick`, `medium`, or `very thorough`). Use it to gather
  context before changing code — e.g. tracing callers of `tts.synthesize`,
  locating route/endpoint handling under `cedar/routes/`, or mapping which tests
  under `tests/` cover a module.
- `general` — general-purpose multi-step work that can run in parallel with my
  own work — e.g. adapting a patch across several files, a bounded refactor, or
  investigating independent questions concurrently.

Routing guidance:

- For small, well-defined, low-risk edits (copy edits, minor styling, a
  straightforward fix with an understood cause, small test updates), do them
  directly — preparing and reviewing a delegation would cost more than the edit.
- For read-only exploration and context-gathering, prefer `explore`.
- For independent, parallelizable, or self-contained multi-step work, delegate
  to `general`.
- The main agent still owns architecture, invasive changes to the synthesis and
  alignment pipeline (`tts.py`), SQLite migrations or rechunking in `db.py`,
  the deny-by-default auth and SSRF-safe fetching boundaries, app-facing API
  compatibility, broad integration, and final review. Do not delegate those.

Model selection:

- Do not set the sub-agent `model` parameter unless the user explicitly asks for
  a specific model or variant. When they do, resolve the exact reference with
  the models tool before passing it.
- If the chosen sub-agent is unavailable, complete the task directly or use an
  available sub-agent suited to the same scope, and briefly report the fallback.
  Never silently substitute a more expensive agent.

Briefs and lifecycle:

- New child sessions start with fresh context, so include all relevant context
  and instructions. Supply a concise, self-contained brief: the request,
  relevant paths, constraints, and acceptance criteria.
- Give the sub-agent a narrow scope and ask it to report changed files, checks
  performed, and unresolved issues. Do not let it delegate further for a simple
  task.
- Run independent sub-agents concurrently (e.g. multiple `subagent` calls in the
  same tool block). Use `background: true` only for independent work that can
  proceed while I continue elsewhere; I am notified on completion and must not
  poll or sleep.
- Pass back the returned `sessionID` to continue a specific sub-agent
  conversation; otherwise a new session starts fresh.
- While a sub-agent implements a change, do useful independent work such as
  inspecting callers, identifying regression risks, or preparing verification.
  Avoid duplicating its implementation or editing the same files concurrently.

Verification and communication:

- Review the actual diff and perform proportionate verification before
  reporting completion — e.g. `ruff check cedar tools tests` plus the relevant
  `pytest tests` subsets for the touched modules; add
  `python tools/lang_selftest.py` / `python tools/align_selftest.py` when
  language or alignment behavior changed. The main agent owns integration,
  correctness, and communication with the user.
- Answer simple conversational questions directly. If delegation tools are
  unavailable, complete the work directly and briefly mention the fallback.
- Do not ask for confirmation merely to use this workflow. Respect the user's
  latest instructions and all applicable permission boundaries.

## GitHub updates

When I ask to "update GitHub," treat that as authorization to commit the requested repository changes and push them to the current branch's configured remote. Run the relevant checks, review the diff, use a descriptive commit message, push, and report the commit and result. Do not pause to ask for confirmation at each step.

Do not interpret "update GitHub" as authorization to create a release, publish assets, force-push, delete branches, or overwrite remote changes. Ask only when the requested action is ambiguous or an actual tool approval is required.
