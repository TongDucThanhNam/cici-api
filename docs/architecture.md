# Architecture and invariants

## Purpose and trust boundary

This project wraps the signed-in Cici/Dola desktop application with a local
FastAPI service and a synchronous CLI. It is browser UI automation, not an
official ByteDance API. UI changes, account quota, and the state of the user's
desktop session are external dependencies.

Keep the service bound to loopback by default. CDP grants control over the
signed-in browser session and must not be exposed to an untrusted network.

## Component map

| Component | Responsibility |
| --- | --- |
| `cici/server.py` | FastAPI schemas and endpoints, in-memory queue/store, worker lifespan (shim `main.py` re-exports it) |
| `cici/driver.py` | CDP attachment, Playwright UI workflow, job model/store, single worker (shim `cici_driver.py` re-exports it) |
| `config.yaml` | CDP settings, selectors, model + option registries, timeouts, concurrency invariant. Shipped as `cici/config.yaml`; installed users edit `~/.cici/config.yaml` (auto-copied on first server boot — resolution order in `cici/_config.py`) |
| `cici/_config.py` | Config resolution: `CICI_CONFIG` env > repo/cwd > `~/.cici/config.yaml` > packaged |
| `cici/_persist.py` | Crash-recovery job persistence: best-effort write-through to `~/.cici/jobs.json`, reconcile in-flight jobs to `FAILED` on boot, 7-day retention pruning of terminal jobs |
| `cici/_client.py` | Synchronous HTTP client, polling, exit-code constants, URL expiry parsing |
| `cici/cli.py` | Click commands (incl. `doctor` prerequisites check), preflight, progress and JSON/human rendering |
| `cici/_launcher.py` | Windows Cici discovery/launch and detached API-server launch (`python -m cici.server`) |
| `cici/_quota.py` | Local rolling 24-hour usage history and learned limit threshold |
| `inspect_dom.py` | Read-only DOM probe for a running CDP session |
| `inspect_result_images.py` | Read-only probe of result-image DOM (preview vs full-size viewer URLs) |
| `inspect_skills.py` | Semi-read-only probe of image/video skill modes (never sends) |
| `tests/test_result_detection.py` | Deterministic fixture-DOM tests of the result-polling JS |
| `tests/stress_test.py` | Full-server stress suite with scriptable fake driver (no Cici, no quota; `~/.cici` state redirected to a temp home) |
| `test_e2e.py` | Live image-generation smoke script; not a hermetic automated test |

## Request and job flow

1. `POST /api/generate` validates the prompt, type, optional model, references,
   ratio/style/duration options, and the local quota estimate.
2. The API stores `PENDING`, places a `Job` on `JOB_QUEUE`, and immediately
   returns HTTP 202 with the job ID.
3. The one worker changes the state to `PROCESSING` and calls
   `CiciDriver.execute`.
4. The driver navigates to the create-image page and clicks the image/video tab
   (build 147.0.7727.149+; legacy skill-bar flow as fallback), always selects
   the model, optionally selects ratio/style/duration, optionally uploads image
   references (image and video — image-to-video), sends the prompt, and polls
   for the result.
5. The store ends in `COMPLETED` (with `result_urls[]`), `FAILED`,
   `QUOTA_EXHAUSTED`, or `CONTENT_BLOCKED`. Clients poll
   `GET /api/status/{job_id}` for the result; `cici/_client.wait_status`
   treats all four as terminal so the CLI reports the right exit code
   immediately instead of polling to timeout.

`JobStore` is process-local in memory, but the server wraps it in a
persistent subclass: every update is written through to `~/.cici/jobs.json`
(best-effort, fail-open), and on boot finished jobs are restored while any
PENDING/PROCESSING job is reconciled to `FAILED` with a clear error so agents
know to retry. The `asyncio.Queue` itself is still in memory — queued jobs do
not survive a restart — and multiple server processes do not share state. Do
not add multiple Uvicorn workers without first replacing these primitives with
shared coordination. Terminal jobs older than 7 days are pruned from the file
on save (retention); result URLs expire anyway, so the file is not durable
media storage.

Jobs are enqueued with a monotonically increasing sequence number; the status
endpoint exposes `queue_ahead` (PENDING jobs enqueued earlier) and `queue_size`
so clients can apply a queue-aware timeout: the CLI's generation timeout counts
only PROCESSING time, while PENDING queue-wait is capped separately at
`timeout × (queue_ahead + 1)`. This keeps N simultaneous CLI callers from
timing out on jobs that are simply waiting their turn.

## Invariants to preserve

- Exactly one consumer controls the Cici chat UI. The queue and the driver's
  `asyncio.Lock` both protect this invariant.
- Result isolation is race-safe by snapshot: the driver captures the bot-message
  count and the media-URL set before sending; polling considers only bot messages
  created after the send (chat branch) or media not present in the snapshot
  (inline branch). Results from an earlier job cannot leak into the current one.
- A successful result requires the completion action bar (chat branch) and at
  least one non-data media URL. Video blocks are clicked to lazy-instantiate the
  `<video>` element before extracting its src. Quota exhaustion is checked before
  success. Image URLs are then upgraded from chat previews to full-size
  originals via the image viewer (see `docs/ui-automation.md`), falling back to
  preview URLs when the viewer is unavailable.
- On an execution error, reload the page when possible so the next queued job
  does not inherit a poisoned UI state. The worker loop itself must survive errors.
- Jobs have two deadlines: the per-kind generation timeout inside
  `_wait_result` and a hard deadline in `run_worker`
  (`timing.<kind>_timeout + timing.hard_deadline_margin`) enforced with
  `asyncio.wait_for`. CDP attach is bounded by `cdp.connect_timeout` so a lost
  Cici fails the current job with a clear error instead of stalling the queue
  forever; a lost connection during polling surfaces to the CLI as
  `POLL_ERROR` (exit 3) with a `cici status <job_id>` hint.
- The local quota estimate must never block generation when its state file is
  corrupt: `QuotaState.from_dict` sanitizes types, `load()` swallows file
  errors, and the `/api/generate` quota pre-check fails open.
- Config resolution (`cici/_config.py`): `CICI_CONFIG` env > `./config.yaml`
  in the CWD (dev workflow) > `~/.cici/config.yaml` (installed users' editable
  copy, auto-created from the packaged default on first server boot) >
  packaged `cici/config.yaml`. Keep `cici/config.yaml` in sync with the
  repo-root file.
- References are local files (image + video modalities) and capped by
  `selectors.ref_max`.
- Model aliases and option aliases (`ratio`/`style`/`duration`) are part of the
  HTTP and CLI contract. Each configured model default must also appear in that
  modality's `options` list.
- Quota tracking is a local estimate stored outside the repository at
  `~/.cici/quota.json`; it is not authoritative account data.
- Result URLs are remote CDN URLs and may expire. Do not treat the in-memory job
  store as durable media storage.

## Change routing

| Change | Primary files | Also review |
| --- | --- | --- |
| Endpoint/schema/job state | `cici/server.py` | `cici/_client.py`, `cici/cli.py`, `README.md` |
| Job persistence/retention | `cici/_persist.py` | Boot-restore logic in `cici/server.py`, `tests/test_persist.py` |
| Browser workflow/result detection | `cici/driver.py` | `config.yaml`, `docs/ui-automation.md` |
| Selector/model/timing update | `config.yaml` | Driver assumptions, CLI help, `README.md` |
| CLI command/output/exit behavior | `cici/cli.py` | `cici/_client.py`, agent integration in `README.md` |
| Auto-launch behavior | `cici/_launcher.py`, launch scripts | platform-specific instructions in `README.md` |
| Quota semantics | `cici/_quota.py` | API 429 handling, CLI exit code 4, `README.md` |
| Packaging/dependency | `pyproject.toml`, `requirements.txt` | install scripts and `README.md` |
