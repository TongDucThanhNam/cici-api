"""Cici browser driver — wraps Playwright CDP connection to the running Cici app.

Single consumer. Hardened:
  - auto-reconnect to CDP if Cici restarted / port dropped
  - per-job timeout -> mark FAILED + reload page to clear zombie state
  - never crashes the worker loop (keeps draining the queue)

Exposes a coroutine `run_worker(queue, status_store, cfg)` that the FastAPI
app launches as a background task on startup.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeoutError,
    async_playwright,
)

# local import (cici package is sibling of cici_driver.py)
try:
    from cici import _quota
except ImportError:  # core có thể chạy standalone (không cài package)
    _quota = None  # type: ignore[assignment]


class QuotaExhausted(Exception):
    """Cici báo hết quota hằng ngày (message detect trong bot response)."""
    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind
        self.raw_message = message

log = logging.getLogger("cici.driver")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------------- #
# Job data (shared with main.py)
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    job_id: str
    kind: str            # "image" | "video"
    prompt: str
    model: str | None = None   # alias trong config.yaml models.<kind>.options[].alias
    created_at: float = field(default_factory=time.time)


@dataclass
class JobStore:
    """In-memory job status. Swap for Redis in production."""
    data: dict[str, dict] = field(default_factory=dict)

    def set(self, job_id: str, **fields) -> None:
        self.data.setdefault(job_id, {}).update(fields)

    def get(self, job_id: str) -> Optional[dict]:
        return self.data.get(job_id)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
class CiciDriver:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sel = cfg["selectors"]
        self.tm = cfg["timing"]
        self._pw = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()  # serialize all UI ops

    # ---- connection lifecycle ------------------------------------------- #
    async def connect(self) -> None:
        cdp = self.cfg["cdp"]
        self._pw = await async_playwright().start()
        await self._attach()

    async def _attach(self) -> None:
        """Connect/reconnect to the running Cici and locate the chat page."""
        cdp = self.cfg["cdp"]
        delay = cdp["reconnect_initial_delay"]
        mx = cdp["reconnect_max_delay"]
        last_err = None
        while True:
            try:
                self._browser = await self._pw.chromium.connect_over_cdp(cdp["endpoint"])
                pat = cdp["chat_url_pattern"]
                page = None
                for ctx in self._browser.contexts:
                    for pg in ctx.pages:
                        if pat in pg.url:
                            page = pg
                if page is None and self._browser.contexts:
                    page = self._browser.contexts[0].pages[-1]
                if page is None:
                    raise RuntimeError("No chat page found in Cici")
                self._page = page
                await page.bring_to_front()
                log.info("Connected to Cici chat page: %s", page.url)
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("CDP connect failed (%s); retry in %ss", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, mx)

    async def _ensure_page(self) -> Page:
        if self._page is None:
            await self._attach()
        try:
            # cheap liveness check
            await self._page.title()  # type: ignore[union-attr]
        except Exception:
            log.warning("Lost Cici page; reconnecting…")
            await self._attach()
        return self._page  # type: ignore[return-value]

    # ---- low-level UI helpers ------------------------------------------- #
    async def _new_conversation(self) -> None:
        page = await self._ensure_page()
        # scope to main: there are two instances (sidebar + main panel)
        await page.get_by_role("main").locator(
            self.sel["new_conversation"]
        ).first.click()
        await asyncio.sleep(self.tm["ui_step_delay"])

    async def _select_skill(self, kind: str) -> None:
        page = await self._ensure_page()
        sel = self.sel["skill_image"] if kind == "image" else self.sel["skill_video"]
        await page.locator(sel).click()
        await asyncio.sleep(self.tm["ui_step_delay"])

    def _resolve_model(self, kind: str, alias: str | None) -> dict | None:
        """Look up model option in cfg['models'][kind]. Returns option dict or None."""
        registry = self.cfg.get("models", {}).get(kind, {})
        if not alias or alias == registry.get("default"):
            return None  # default already selected in UI, no action needed
        for opt in registry.get("options", []):
            if opt["alias"] == alias:
                return opt
        raise ValueError(
            f"Unknown model '{alias}' for kind '{kind}'. "
            f"Valid: {[o['alias'] for o in registry.get('options', [])]}"
        )

    async def _select_model(self, kind: str, alias: str | None) -> None:
        """Open model dropdown + click option matching alias. No-op if default/None."""
        opt = self._resolve_model(kind, alias)
        if opt is None:
            return
        page = await self._ensure_page()
        # Click "Model" button to open dropdown
        await page.get_by_role("main").locator(self.sel["model_button"]).first.click()
        await asyncio.sleep(0.6)
        # Click the option whose text contains select_text
        option = page.locator(self.sel["model_option"], has_text=opt["select_text"]).first
        await option.click()
        await asyncio.sleep(self.tm["ui_step_delay"])

    async def _type_prompt(self, prompt: str) -> None:
        """In skill mode the input is a contenteditable TiPTap; fallback to textarea."""
        page = await self._ensure_page()
        editor = page.get_by_role("main").locator(self.sel["editor_prose"]).last
        try:
            await editor.wait_for(state="visible", timeout=8000)
            await editor.click()
            await asyncio.sleep(0.3)
            await page.get_by_role("main").locator(self.sel["editor_prose"]).last.type(
                prompt, delay=8
            )
        except (PWTimeoutError, Exception):
            ta = page.get_by_role("main").locator(self.sel["chat_textarea"]).last
            await ta.wait_for(state="visible", timeout=10000)
            await ta.fill(prompt)

    async def _send(self) -> None:
        page = await self._ensure_page()
        await page.get_by_role("main").locator(self.sel["send_button"]).click()

    async def _wait_result(self, timeout: float, kind: str = "image") -> list[str]:
        """Poll message-list; return list of generated media URLs when done.

        'Done' = the newest bot message contains an action bar (data-testid
        message_action_bar) AND at least one result image.
        Raises QuotaExhausted nếu bot message chứa text báo hết quota hằng ngày.
        """
        page = await self._ensure_page()
        deadline = time.time() + timeout
        interval = self.tm["poll_interval"]
        last_count = -1
        while time.time() < deadline:
            res = await page.evaluate(
                r"""(sel) => {
                    const ml = document.querySelector(sel.message_list);
                    if (!ml) return {recv: 0, done: false, urls: [], text: ''};
                    const recvs = ml.querySelectorAll(sel.bot_message);
                    const last = recvs[recvs.length - 1];
                    if (!last) return {recv: recvs.length, done: false, urls: [], text: ''};
                    const done = !!last.querySelector(sel.done_indicator);
                    const imgs = Array.from(last.querySelectorAll(sel.result_image))
                        .map(i => i.currentSrc || i.src || '')
                        .filter(s => s && !s.startsWith('data:'));
                    return {recv: recvs.length, done, urls: imgs, text: (last.innerText||'').slice(0,400)};
                }""",
                self.sel,
            )
            # detect quota-exhausted message BEFORE treating as success/timeout
            if _quota and res.get("text") and _quota.is_exhausted_message(res["text"]):
                raise QuotaExhausted(res["text"], kind)
            if res["urls"] and (res["done"] or len(res["urls"]) >= 1):
                # require "done" if present, else settle for >=1 image
                if res["done"]:
                    return res["urls"]
                if len(res["urls"]) > last_count and len(res["urls"]) >= 1:
                    last_count = len(res["urls"])
            await asyncio.sleep(interval)
        raise TimeoutError(f"No result within {timeout}s")

    # ---- high-level job execution --------------------------------------- #
    async def execute(self, job: Job) -> dict[str, Any]:
        async with self._lock:
            timeout = (
                self.tm["image_timeout"] if job.kind == "image" else self.tm["video_timeout"]
            )
            try:
                await self._new_conversation()
                await self._select_skill(job.kind)
                await self._select_model(job.kind, job.model)
                await self._type_prompt(job.prompt)
                await self._send()
                urls = await self._wait_result(timeout, kind=job.kind)
                # success → record quota
                if _quota:
                    state = _quota.load()
                    _quota.record_success(state, job.kind)
                    _quota.save(state)
                return {
                    "status": "COMPLETED",
                    "result_urls": urls,
                    "kind": job.kind,
                    "model": job.model or self.cfg["models"][job.kind]["default"],
                }
            except QuotaExhausted as e:
                # quota hết → record limit-hit (auto-learn threshold)
                if _quota:
                    state = _quota.load()
                    thr = _quota.record_limit_hit(state, job.kind)
                    _quota.save(state)
                    log.warning("Job %s QUOTA EXHAUSTED (kind=%s, learned threshold=%s)",
                                job.job_id, job.kind, thr)
                return {
                    "status": "QUOTA_EXHAUSTED",
                    "kind": job.kind,
                    "message": e.raw_message,
                    "quota": _quota.snapshot(_quota.load(), job.kind) if _quota else None,
                }
            except Exception as e:  # noqa: BLE001
                log.error("Job %s failed: %s", job.job_id, e)
                # clear zombie state so the next job isn't poisoned
                try:
                    page = await self._ensure_page()
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                return {"status": "FAILED", "error": str(e)}


# --------------------------------------------------------------------------- #
# Worker loop (consumer)
# --------------------------------------------------------------------------- #
async def run_worker(
    queue: "asyncio.Queue[Job]",
    store: JobStore,
    cfg: dict,
) -> None:
    driver = CiciDriver(cfg)
    log.info("Worker starting; connecting to Cici…")
    await driver.connect()
    log.info("Worker ready, waiting for jobs.")
    while True:
        job: Job = await queue.get()
        store.set(job.job_id, status="PROCESSING", started_at=time.time())
        log.info("Processing job %s (%s)", job.job_id, job.kind)
        try:
            result = await driver.execute(job)
            store.set(job.job_id, finished_at=time.time(), **result)
        except Exception as e:  # noqa: BLE001  never kill the loop
            log.exception("Unhandled worker error on job %s", job.job_id)
            store.set(job.job_id, status="FAILED", error=str(e), finished_at=time.time())
        finally:
            queue.task_done()
