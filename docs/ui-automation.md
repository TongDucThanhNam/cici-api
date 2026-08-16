# Cici UI automation

Read this guide before changing `cici/driver.py`, CDP/selector/model/timing fields
in `config.yaml`, `inspect_dom.py`, or launcher behavior.

## Safety boundary

CDP controls the user's signed-in Cici browser session. Keep the endpoint on
loopback and never log or commit profile/session data. Do not terminate or
relaunch Cici, click/type in the session, upload files, or submit generation
unless the task explicitly requires that side effect.

`inspect_dom.py` is the preferred first diagnostic because it only evaluates DOM
state, although it does focus the selected page. `inspect_skills.py` is the
semi-read-only skill-mode probe: it enters the image/video skill modes (clicks
skill buttons/tabs and opens dropdowns) but never types a prompt or sends — no
generation, no quota. `start_cici.bat` is not a probe: it terminates and
relaunches Cici and therefore requires explicit need.

## Supported build (2026-08)

Driver entry point is the dedicated creation page **"Tác phẩm của AI"**
(`chrome://dola-chat/chat/create-image`, `cdp.create_image_url`), verified on
Dola build **Chrome/147.0.7727.149**:

**Doubao (豆包) provider** — the Chinese sibling of the same app (same Chromium
build, mirrored URLs `chrome://doubao-chat/chat/create-image`, identical
`data-testid`s incl. creation tabs, reference button, TiPTap editor). Probed
2026-08: toolbar 模型/比例/风格, models Seedream 5.0 Pro / 5.0 Lite / 4.5 / 4.0
(image, quota multipliers 4x/3x) and Seedance 2.5 / 2.0 / Fast / Mini (video),
32 Chinese styles, ratios include 自动 (`auto`) + `3:2` (image) / `21:9`
(video). Two Doubao-specific behaviors: (1) the app must be launched via the
stub `Application/Doubao.exe` — launching `app/Doubao.exe` directly ignores
`--remote-debugging-port`; (2) the ratio/duration picker renders plain
`button`s (no `role="menuitem"`), so `_select_dropdown` falls back to clicking
by exact accessible name (`_has_text(..., exact=True)` anchors the regex). No
duration picker exists (merged into the ratio button label) — `--duration` is
rejected server-side for `provider=doubao`.

- Tabs `creation-skill-switch-tab-image` / `-video` switch modality. Both tabs
  share the same reference button (`image-creation-chat-input-picture-reference-button`),
  which opens a native file chooser.
- Image tab toolbar: Model (`Seedream 5.0 Pro` / `4.5`), Ratio/Tỷ lệ (6 ratios),
  Style/Phong cách (13 styles). Video tab: Model (3 Seedance), duration button
  showing `5s`/`10s`, Ratio (6 ratios, includes `21:9`). The UI locale switches
  between Vietnamese and English (observed EN after an app restart): toolbar
  buttons are matched by bilingual `has-text` selectors in `config.yaml`
  (`ratio_button`/`style_button`), and style `select_text` entries are lists of
  VI/EN alternatives compiled to a regex by `CiciDriver._has_text`. These
  buttons expose no stable `data-testid`/ARIA attribute (Radix-generated ids),
  so localized text is currently the only handle — re-probe with
  `inspect_dom.py` if a locale change breaks them again.
- The model button shows the **last-selected** model, not a fixed default
  (observed: video tab opened on `Model 1.0`). The driver therefore always
  clicks the target model option, including the configured default.
- Fresh conversations no longer show a skill bar; the "Kỹ năng" menu is a
  one-shot first-run hint. If the create-image page/tabs are missing (older
  build), `execute()` falls back to the legacy skill-bar flow
  (`skill_bar_button_3/17` inside a skill conversation).
- Video results render as `div[class*="block-video"]` blocks containing a cover
  `img`; the `<video>` element (src on `v16-dola.dola.com`) is lazily created by
  xgplayer **only after the block is clicked**. `_wait_result` clicks unloaded
  blocks and reads `video.src` on a later poll. Video URLs carry no `x-expires`.
- Chat image results render two `<img>` per `mdbox_image`: an SVG placeholder
  declaring the full dimensions plus the preview (`~tplv-…-downsize_watermark`,
  ~288px). The **full-size original** (`~tplv-…-image_pre_watermark`, e.g.
  1773×2364) is only instantiated when the image viewer is open, one image at
  a time (arrow keys do NOT navigate; siblings are only network-prefetched).
  After `_wait_result` succeeds on an image job, `_upgrade_to_fullsize` clicks
  each result box, reads the viewer URL (DOM via `_FULLSIZE_JS` + page request
  listener), presses Escape, and repeats — matching each URL by its base path
  (before `~tplv`) so other jobs' images can't leak in. Marker lives in
  `selectors.fullsize_image_marker`; timings in `timing.fullsize_wait` /
  `fullsize_each_wait` / `viewer_close_delay`. Any failure falls back to the
  preview URLs — the job still completes.

## Selector-change workflow

1. Confirm the affected Cici build and reproduce the failure without concurrent
   jobs.
2. Connect to the intended loopback CDP session and run `python inspect_dom.py`.
3. Prefer stable `data-testid`, ARIA role, or semantic attributes. Scope locators
   to the main region when duplicate sidebar/main controls exist. Avoid generated
   CSS classes and localized visible text when a stable attribute exists.
4. Change selector or model text values in `config.yaml`. Change Python only when
   the interaction sequence, fallback strategy, or result semantics changed.
5. Preserve timeouts and fallbacks unless observation shows they are obsolete.
6. Verify the narrow interaction first. Run a full generation only when explicitly
   required, then update the supported-build note or known limitations as needed.

The model registry is not discovered from an API. For every option, keep `alias`,
display `name`, and exact UI `select_text` aligned, and ensure `default` names a
listed alias. Changing an alias is a public API/CLI compatibility change.

## Interaction invariants

- Keep all UI operations serialized through the queue and driver lock.
- Entry: navigate to `cdp.create_image_url` and click the image/video tab
  (`_enter_creation_page`); fall back to the legacy new-conversation + skill-bar
  flow when the page/tab is unavailable. Do not rely on the fresh-conversation
  skill bar on build 147.0.7727.149+.
- Capture the bot-message count and the current media-URL snapshot before
  sending. Chat result polling must inspect only messages added after that
  snapshot; inline (no message-list) polling diffs media against the snapshot.
- Do not report success until the current message has both its completion marker
  and at least one relevant non-data media URL (inline: media appeared and is
  stable across two consecutive polls).
- Detect quota-exhaustion text before success/timeout handling so the quota
  tracker can learn the current limit.
- Retain reconnect backoff for a lost CDP target and best-effort page reload after
  job failure. A job error must not kill the worker loop.
- Reference uploads support image **and video** (image-to-video). Two upload
  paths, tried in order: the Templates modal ("Templates"/"Mẫu") file input, then
  the reference-button native file-chooser. On build 147.0.7727.149 the
  create-image page has no Templates modal — the file-chooser path is primary.
  Preserve both until both supported builds have been checked.

## Extending result support

Image detection uses `selectors.result_image`; video uses
`selectors.result_video` (block containers, lazy `<video>`). Result polling has
two branches: chat (`message-list` present — app navigated to a conversation)
and inline (still on the create-image page). Adding or repairing media support
must cover the complete contract:

1. Add the media selector to `config.yaml` and observe it in a completed current
   bot message.
2. Update `_wait_result` / `_POLL_RESULT_JS` without weakening message isolation,
   completion, or quota checks. Video block clicks must only target the new
   job's messages (chat branch) or not yet instantiated blocks.
3. Return a consistent URL list and terminal state through the worker/store.
4. Confirm API polling, synchronous client termination, CLI text/JSON rendering,
   timeout values, and exit codes all handle the state.
5. Update `README.md` and extend `tests/test_result_detection.py` (deterministic
   fixture-DOM tests of the exact poll JS) before an explicitly authorized live
   smoke.

## Operational diagnostics

- `GET http://127.0.0.1:9222/json/version` checks CDP reachability without a
  generation request.
- `GET /api/health` reports CDP reachability and queue size; an `unreachable`
  payload is diagnostic data, not necessarily an application crash.
- The API can serve while the background worker waits in reconnect backoff; job
  processing is not ready until the worker attaches to Cici.
- The CLI generation timeout is slightly longer than the driver's timeout so the
  worker has time to persist a terminal state.
- Generated media and screenshots are ignored by Git, but still may contain
  sensitive user content. Keep them out of the repository unless the task
  explicitly requires a sanitized fixture.
