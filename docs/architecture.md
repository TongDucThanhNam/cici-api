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
| `cici/server.py` | FastAPI schemas/endpoints, queue + persistent-store composition, worker lifespan (shim `main.py` re-exports it) |
| `cici/jobs.py` | Framework-free job model, status vocabulary, in-memory store port, queue-position calculation |
| `cici/worker.py` | Single-consumer application service, hard deadlines, failure isolation; concrete driver is injected by `server.py` |
| `cici/driver.py` | CDP attachment and serialized Playwright UI workflow (shim `cici_driver.py` preserves old imports) |
| `cici/catalog.py` | The one provider-aware view over legacy Cici and nested provider model/option registries |
| `cici/_interaction.py` | Pure refusal/confirmation classification and automatic-reply policy used by the driver |
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
| `tests/test_architecture.py` | Hermetic contracts for jobs, catalog lookup, injected worker, and `ast-tree` command construction |
| `tests/stress_test.py` | Full-server stress suite with scriptable fake driver (no Cici, no quota; `~/.cici` state redirected to a temp home) |
| `test_e2e.py` | Live image-generation smoke script; not a hermetic automated test |

## Layering and dependency direction

Keep dependencies pointing inward. Compatibility facades may re-export names,
but responsibility remains with the owning module.

| Layer | Modules | May depend on |
| --- | --- | --- |
| Domain contracts | `cici/jobs.py` | Python standard library only |
| Configuration policy | `cici/catalog.py`, `cici/_interaction.py` | Python standard library and plain config dictionaries |
| Application service | `cici/worker.py` | domain contracts plus an injected driver protocol |
| Adapters | `cici/server.py`, `cici/driver.py`, `cici/_client.py`, `cici/cli.py` | application/domain modules and their own framework |
| Infrastructure support | `cici/_persist.py`, `cici/_quota.py`, `cici/_launcher.py`, `cici/_config.py` | domain contracts where needed; never CLI presentation |

The checked-in AST rules in `ast-grep/rules/` mechanically protect framework
boundaries and the single-consumer configuration. See
`docs/code-navigation.md` for the progressive exploration workflow.

## Request and job flow

1. `POST /api/generate` validates the prompt, type, optional provider
   (`cici` default | `doubao`), optional model, references, ratio/style/duration
   options (against that provider's registry), and the local quota estimate
   (per provider + account).
2. The API stores `PENDING`, places a `Job` on `JOB_QUEUE`, and immediately
   returns HTTP 202 with the job ID.
3. The one `cici.worker` consumer changes the state to `PROCESSING` and calls
   the injected `CiciDriver.execute` adapter.
4. The driver switches CDP endpoint when the job's provider differs from the
   attached one (Cici 9222 / Doubao 9223 — `cdp.providers.<name>` overlay;
   disconnect only, never kills the app), then navigates to the create-image
   page and clicks the image/video tab (build 147.0.7727.149+; legacy skill-bar
   flow as fallback), always selects the model, optionally selects
   ratio/style/duration, optionally uploads image references (image and video —
   image-to-video), sends the prompt, and polls for the result.
5. The store ends in `COMPLETED` (with `result_urls[]`), `FAILED`,
   `QUOTA_EXHAUSTED`, or `CONTENT_BLOCKED`. Clients poll
   `GET /api/status/{job_id}` for the result; `cici/_client.wait_status`
   treats all four as terminal so the CLI reports the right exit code
   immediately instead of polling to timeout.

### Providers

Both apps are the same ByteDance codebase (Chromium 147.0.7727.149) with
mirrored URLs (`chrome://dola-chat/...` vs `chrome://doubao-chat/...`) and the
same `data-testid`s. Provider differences live in `config.yaml`:
`providers.<name>` (exe path/env, CDP port, chat host), `cdp.providers.<name>`
(URL overlay), and per-provider registries under `models.<provider>` /
`options.<provider>` (legacy flat keys remain the `cici` registry). Quota state
is namespaced per provider (`~/.cici/quota-doubao*.json`; cici keeps legacy
`quota*.json` names). Doubao quirks: it must be launched via the stub
`Application/Doubao.exe` for the CDP flag to be forwarded, and its ratio picker
renders plain buttons (not `role="menuitem"`) — `_select_dropdown`
auto-detects this and clicks by exact accessible name.

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
  `_wait_result` and a hard deadline in `cici.worker.run_worker`
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
| Job fields/status/store/queue position | `cici/jobs.py` | `cici/worker.py`, `cici/server.py`, persistence/client contracts |
| Worker deadline/failure behavior | `cici/worker.py` | `cici/driver.py` compatibility facade, server lifespan, stress tests |
| Job persistence/retention | `cici/_persist.py` | Boot-restore logic in `cici/server.py`, `tests/test_persist.py` |
| Provider/model/option lookup shape | `cici/catalog.py`, `config.yaml` | API validation, driver selection, provider tests |
| Bot refusal/confirmation policy | `cici/_interaction.py` | Polling orchestration in `cici/driver.py`, message config, result tests |
| Browser workflow/result detection | `cici/driver.py` | `config.yaml`, `docs/ui-automation.md` |
| Selector/model/timing update | `config.yaml` | Catalog/driver assumptions, CLI help, `README.md` |
| CLI command/output/exit behavior | `cici/cli.py` | `cici/_client.py`, agent integration in `README.md` |
| Auto-launch behavior | `cici/_launcher.py`, launch scripts | platform-specific instructions in `README.md` |
| Quota semantics | `cici/_quota.py` | API 429 handling, CLI exit code 4, `README.md` |
| Packaging/dependency | `pyproject.toml`, `requirements.txt` | install scripts and `README.md` |
