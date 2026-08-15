# Testing and verification

## Current test boundary

Hermetic suites (no Cici, no quota, no live generation — safe to run any time
from the repository root):

| Suite | What it exercises |
| --- | --- |
| `python tests/test_wait_status.py` | Queue-aware client polling, adaptive backoff, terminal-state short-circuit |
| `python tests/test_result_detection.py` | The exact result-polling/full-size JS against fixture DOMs |
| `python tests/test_quota.py` | Quota rolling window, limit classification, `plan_retry`, CLI `--wait-for-quota` loop (mocked HTTP) |
| `python tests/test_accounts.py` | Account-label quota isolation and CLI `--account` plumbing |
| `python tests/test_persist.py` | Job persistence: fail-open loads, ephemeral-key stripping, boot reconcile, retention pruning |
| `python tests/stress_test.py` | The real FastAPI server + worker + CLI subprocesses with a scriptable fake driver; redirects `~/.cici` state into a temp home so it never touches user quota/job files |

`test_e2e.py` remains a live smoke script: it expects the API and a signed-in
Cici CDP session, submits a real image job, consumes quota, polls for up to
320 seconds, and requires a human to inspect the printed result. It is not
safe as a default validation command.

`inspect_dom.py` does not type or click, but it connects to the user's browser
session and brings the selected Cici page to the foreground.

## Default verification ladder

Run checks from the repository root and stop at the smallest level that provides
meaningful evidence for the change.

1. For every patch, inspect `git diff --check` and `git diff --stat`.
2. For Python edits, compile the touched modules. A repository-wide syntax check is:

   ```powershell
   python -m compileall -q main.py cici_driver.py cici tests
   ```

3. For import, schema, or route changes, import the application if dependencies
   are installed:

   ```powershell
   python -c "import main; print([r.path for r in main.app.routes])"
   ```

4. For pure client, quota, parsing, or validation logic, add focused deterministic
   tests before relying on a live browser. Keep network, filesystem, clock, and
   home-directory state injectable or isolated.
5. For server integration, start Uvicorn on loopback and exercise only the changed
   non-generation endpoints first. `/api/health` may validly report
   `unreachable` when Cici is not running.
6. Run live DOM or generation checks only when the task explicitly requires them
   and all prerequisites below are met.

Documentation-only changes normally need link/path inspection plus the diff
checks; they do not justify starting Cici or consuming generation quota.

## Change-specific expectations

| Area changed | Minimum useful evidence |
| --- | --- |
| Documentation/config comments | Diff checks; verify referenced files and commands exist |
| Pure helper/client/quota logic | Syntax check plus focused deterministic tests |
| API schema or endpoint | Syntax/import check plus request/response test without generation where possible |
| CLI behavior | Invoke the changed command through Click with network/browser calls mocked or isolated |
| Selector/model mapping | DOM inspection against the relevant Cici build; record what was observed |
| Browser workflow/result detection | Focused mocked logic where possible, then one explicitly authorized live smoke |
| Launcher/install script | Platform-appropriate dry inspection and isolated process/path tests; do not modify user state casually |

## Live-test prerequisites

Before `python test_e2e.py`, `cici image`, or `cici video`:

- Confirm the user requested a live generation check and understands it consumes
  account quota.
- Confirm Cici is signed in and intentionally running with CDP on
  `127.0.0.1:9222`; do not expose the port externally.
- Confirm the API is running from the repository root on the expected base URL.
- Use a non-sensitive prompt and only explicitly approved local reference files.
- Allow at least the configured timeout and avoid submitting another job in
  parallel.
- Report the terminal job state and elapsed time. A printed response or exit code
  alone is not proof that the generated media is correct.

Never reset `~/.cici/quota.json`, terminate/relaunch the user's Cici process, or
discard a signed-in profile merely to make a test pass.

## Reporting

State exactly which checks ran, their outcome, and why any stronger check was not
run. Do not describe a live path as verified when only syntax/import checks ran,
and do not treat an unavailable Cici session as a code failure without evidence.
