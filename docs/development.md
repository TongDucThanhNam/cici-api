# Development guide

## Prerequisites and setup

- Python 3.10 or newer.
- Cici/Dola installed and signed in only for live CDP or generation work.
- Windows is the supported auto-launch path. macOS/Linux require a manually
  launched Cici instance with CDP enabled.

From the repository root, install core and CLI dependencies:

```powershell
python -m pip install -r requirements.txt
python -m pip install -e .
```

For contributor AST navigation and architecture checks, install the optional
developer extra instead of the second command above:

```powershell
python -m pip install -e ".[dev]"
ast-tree --help
```

The platform installers are `install.ps1` and `install.sh`. Avoid running an
installer merely to inspect or edit the project because it mutates the user's
Python environment and, on Windows, may modify the user `PATH`.

## Local operation

Start the API (self-contained; config resolves from the repo root when
running from the repo, else `~/.cici/config.yaml`):

```powershell
python -m cici.server          # hoặc: python -m uvicorn cici.server:app --port 8000
```

Useful non-generation checks:

```powershell
cici health --json
cici models --json
cici quota --json
```

`cici health` reports state without auto-launching. Image and video commands may
auto-launch Cici and the core server unless `--no-auto-launch` is supplied.

See `README.md` for the complete user-facing runbook, raw HTTP examples, CLI
commands, models, and exit codes.

## Navigation and change workflow

1. Start with `rg --files` and targeted `rg -n '<pattern>'` searches.
2. Use `ast-tree map <path>` for an unfamiliar subtree and
   `ast-tree show <file> --symbol '<symbol>'` for a known owner before reading
   a large source file. The wrapper delegates to `ast-grep outline`.
3. Use source reads, an LSP, or the compiler to confirm behavior; an outline is
   syntax-only and does not establish references or runtime semantics.
4. Trace contracts across API, client, CLI, config, and user documentation using
   the change-routing table in `architecture.md`.

The complete broad-to-narrow command ladder, structural-search examples, and
checked-in architecture rules live in [code-navigation.md](code-navigation.md).

## Python and API conventions

- Keep Python compatible with 3.10. Do not introduce newer syntax or standard
  library APIs without raising `requires-python` intentionally.
- The FastAPI and Playwright paths are asynchronous. The packaged HTTP client and
  Click CLI are deliberately synchronous.
- Keep browser details behind `CiciDriver`; keep Click/Rich presentation out of
  `cici/_client.py` so client logic remains independently testable.
- Keep job/status contracts in `cici/jobs.py`, provider registry shape in
  `cici/catalog.py`, and worker deadline/failure policy in `cici/worker.py`.
  Inject adapters into the worker rather than importing a concrete browser
  implementation into the application-service layer.
- Validate requests before enqueueing. Preserve the immediate HTTP 202 response
  and polling model for long-running generation.
- Keep user-visible status strings and CLI exit codes stable. If a new terminal
  state is added, update the worker, API client polling, both CLI render modes,
  tests, and `README.md` together.
- JSON mode writes its final machine-readable value to stdout; progress belongs
  on stderr. Do not mix decorative output into JSON stdout.
- Log job IDs and short diagnostic context, not secrets, full signed URLs, or
  unnecessary prompt/reference contents.

## Configuration and dependencies

Put UI selectors, model labels/aliases, CDP parameters, and timeouts in
`config.yaml` unless code must support a genuinely different interaction flow.
Local overrides and secrets belong in ignored files, not committed defaults.

Dependency ownership:

- `requirements.txt` and `pyproject.toml` both install the API, driver, and CLI
  runtime. Since v0.3.0 the wheel is self-contained (server + config ship inside
  the `cici` package), so `pipx install` alone is enough for end users.
- A new `cici/` runtime dependency belongs in both files.
- Contributor-only tools belong in the `dev` optional dependency group; do not
  add them to `requirements.txt` or the runtime dependency list.
- `cici/config.yaml` must stay byte-identical to the repo-root `config.yaml`
  (the stress suite checks this) — copy it before building a wheel.
- Runtime user state lives under `~/.cici/` (`config.yaml`, `quota.json`,
  `quota-<account>.json`, `jobs.json`, `server.log`). Never commit it. The
  stress suite redirects this directory into a temp home before importing any
  `cici` module — keep new module-level state paths under `Path.home() / ".cici"`
  so they inherit the same isolation.

When behavior changes, update docstrings/comments only where they explain a
non-obvious invariant. Update `README.md` for public commands, endpoints, payloads,
models, setup, or limitations; update these agent guides for contributor workflow
or architecture changes.
