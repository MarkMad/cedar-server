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

## Delegate tasks

The main agent should orchestrate delegated work and remain responsible for integration, verification, and the final result. Choose the model directly from the task’s complexity; do not use an automatic model escalation ladder.

- For small, well-defined, low-risk implementation tasks, delegate one bounded change to `gpt-6-luna` with `reasoning_effort: "low"` when available. Examples include copy edits, minor styling changes, straightforward bug fixes with an understood cause, and small test updates.
- For moderate multi-file changes, debugging, or adapting patches, delegate to `gpt-6.1-sol` with `reasoning_effort: "medium"` when available. Use `reasoning_effort: "high"` for bounded tasks requiring deeper analysis, such as concurrency bugs or transaction correctness. Prefer this current workhorse over the previous-generation `gpt-6-sol`.
- For exceptionally demanding reasoning or an independent review of a complex design, use `gpt-6-astra` with `reasoning_effort: "high"` when the task justifies it. Do not select Astra merely because it is available; the main agent still owns architecture, sensitive fork behavior, broad integration, and final review.
- Choose only model IDs and reasoning efforts supported by the current delegation tool. These are task-routing preferences, not an escalation ladder. If the selected model is unavailable, complete the task directly or use an available model suited to the same scope, and briefly report the fallback. Never silently substitute a more expensive agent.
- With `collaboration.spawn_agent`, use `fork_turns: "none"` and supply a concise, self-contained brief containing the request, relevant paths, constraints, and acceptance criteria.
- While the sub-agent implements the change, do useful independent work such as inspecting callers, identifying regression risks, or preparing verification. Avoid duplicating its implementation or editing the same files concurrently.
- Give the sub-agent a narrow scope and ask it to report changed files, checks performed, and unresolved issues. Do not let it delegate further for a simple task.
- Review its actual diff and perform proportionate verification before reporting completion. The main agent owns integration, correctness, and communication with the user.
- Answer simple conversational questions directly. If delegation tools or the selected model are unavailable, complete the work directly and briefly mention the fallback.
- Handle trivial edits directly when preparing and reviewing a delegation would take more work than the edit itself.
- Do not ask for confirmation merely to use this workflow. Respect the user's latest instructions and all applicable permission boundaries.

## GitHub updates

When I ask to "update GitHub," treat that as authorization to commit the requested repository changes and push them to the current branch's configured remote. Run the relevant checks, review the diff, use a descriptive commit message, push, and report the commit and result. Do not pause to ask for confirmation at each step.

Do not interpret "update GitHub" as authorization to create a release, publish assets, force-push, delete branches, or overwrite remote changes. Ask only when the requested action is ambiguous or an actual tool approval is required.
