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

The platform installers are `install.ps1` and `install.sh`. Avoid running an
installer merely to inspect or edit the project because it mutates the user's
Python environment and, on Windows, may modify the user `PATH`.

## Local operation

Start the API from the repository root so `config.yaml` resolves correctly:

```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8000
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
2. Run `ast-grep outline <path>` before reading a large or unfamiliar source
   file. For a known symbol, use
   `ast-grep outline <path> --match '<symbol>' --view expanded`.
3. Use source reads, an LSP, or the compiler to confirm behavior; an outline is
   syntax-only and does not establish references or runtime semantics.
4. Trace contracts across API, client, CLI, config, and user documentation using
   the change-routing table in `architecture.md`.

## Python and API conventions

- Keep Python compatible with 3.10. Do not introduce newer syntax or standard
  library APIs without raising `requires-python` intentionally.
- The FastAPI and Playwright paths are asynchronous. The packaged HTTP client and
  Click CLI are deliberately synchronous.
- Keep browser details behind `CiciDriver`; keep Click/Rich presentation out of
  `cici/_client.py` so client logic remains independently testable.
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

Dependency ownership is split:

- `requirements.txt` installs the API, driver, and CLI runtime.
- `pyproject.toml` declares dependencies needed by the distributable `cici` CLI
  package.
- A new `cici/` runtime dependency normally belongs in both files. A server-only
  dependency belongs in `requirements.txt`. Keep install scripts and README setup
  instructions consistent with either change.

When behavior changes, update docstrings/comments only where they explain a
non-obvious invariant. Update `README.md` for public commands, endpoints, payloads,
models, setup, or limitations; update these agent guides for contributor workflow
or architecture changes.
