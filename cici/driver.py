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
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeoutError,
    async_playwright,
)

# quota tracking cùng package (CLI cài riêng vẫn chạy được core standalone)
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


class ContentBlocked(Exception):
    """Cici ĐÃ gen xong nhưng TỪ CHỐI hiển thị kết quả (bản quyền / content policy).

    Match refusal patterns (config.yaml messages.refusal_patterns) trong bot
    response. Driver fail nhanh thay vì spin tới timeout.
    """
    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind
        self.raw_message = message


# Fallback refusal patterns nếu config thiếu messages.refusal_patterns.
# (Wording Cici có thể đổi — thêm vào config.yaml khi thấy pattern mới.)
_DEFAULT_REFUSAL_PATTERNS = [
    "bảo vệ bản quyền",
    "bản quyền",
    "to protect copyright",
    "copyright",
]

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
    references: list[str] = field(default_factory=list)  # local file paths for reference-image gen
    ratio: str | None = None       # alias config.yaml options.<kind>.ratios[].alias (vd "16:9")
    style: str | None = None       # alias options.image.styles[].alias (image only)
    duration: str | None = None    # alias options.video.durations[].alias (video only, "5s"/"10s")
    account: str | None = None     # nhãn tách quota local (user TỰ đổi account trong app — tool không tự đổi)
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
# Result-polling JS (module constants — importable để test deterministic)
# --------------------------------------------------------------------------- #
# Snapshot trước khi send: số bot messages hiện có + set media URL hiện có trên
# trang (để nhánh inline diff ra media MỚI xuất hiện sau send).
_SNAPSHOT_JS = r"""({sel}) => {
    const ml = document.querySelector(sel.message_list);
    const before = ml ? ml.querySelectorAll(sel.bot_message).length : 0;
    const media = [];
    document.querySelectorAll(sel.result_image).forEach(i => {
        const s = i.currentSrc || i.src || '';
        if (s && !s.startsWith('data:')) media.push(s);
    });
    document.querySelectorAll(sel.result_video).forEach(b => {
        const v = b.querySelector('video');
        if (v) {
            const s = v.currentSrc || v.src || '';
            if (s && !s.startsWith('data:') && !s.startsWith('blob:')) media.push(s);
        }
    });
    return {before, media};
}"""

# Poll kết quả. Hai nhánh:
#   A. chat: trang có message-list (sau send app navigate sang conversation mới).
#      Chỉ nhìn bot messages MỚI (index >= before) — race-safe. Collect ảnh
#      (result_image) + video (result_video): <video> lazy-init khi click block,
#      nên poll này click các block chưa có player; poll sau đọc src.
#      'Done' = bot message MỚI NHẤT có action bar.
#   B. inline: trang không có message-list (kết quả hiện ngay trên trang
#      create-image). Diff media hiện có với snapshot trước send -> media mới.
# Trả text (bot messages mới / body text) để detect quota-exhausted TRƯỚC khi
# coi là success/timeout.
_POLL_RESULT_JS = r"""({sel, before, mediaBefore, kind}) => {
    const collectVideos = (root) => {
        const out = [];
        root.querySelectorAll('video').forEach(v => {
            const s = v.currentSrc || v.src || '';
            if (s && !s.startsWith('data:') && !s.startsWith('blob:')) out.push(s);
        });
        root.querySelectorAll('video source').forEach(v => {
            const s = v.src || '';
            if (s && !s.startsWith('data:') && !s.startsWith('blob:')) out.push(s);
        });
        return out;
    };
    const clickUnloadedVideoBlocks = (root) => {
        root.querySelectorAll(sel.result_video).forEach(b => {
            if (!b.querySelector('video')) {
                try { b.click(); } catch (e) {}
            }
        });
    };
    const ml = document.querySelector(sel.message_list);
    if (ml) {
        // --- branch A: conversation message list ---
        const recvs = Array.from(ml.querySelectorAll(sel.bot_message));
        const newOnes = recvs.slice(before);
        if (newOnes.length === 0)
            return {mode: 'chat', recv: recvs.length, newRecv: 0, done: false, urls: [], text: ''};
        const last = newOnes[newOnes.length - 1];
        let done = !!last.querySelector(sel.done_indicator);
        const imgs = [];
        const videos = [];
        let videoBlocks = 0;
        newOnes.forEach(m => {
            m.querySelectorAll(sel.result_image).forEach(i => {
                const s = i.currentSrc || i.src || '';
                if (s && !s.startsWith('data:')) imgs.push(s);
            });
            videoBlocks += m.querySelectorAll(sel.result_video).length;
            videos.push(...collectVideos(m));
        });
        if (kind === 'video') {
            if (videos.length === 0 && videoBlocks > 0) {
                newOnes.forEach(m => clickUnloadedVideoBlocks(m));
            }
            const text = newOnes.map(m => (m.innerText || '')).join('\n').slice(0, 800);
            return {
                mode: 'chat', recv: recvs.length, newRecv: newOnes.length,
                done: done && videos.length > 0,
                urls: videos, videoBlocks, text,
            };
        }
        const text = newOnes.map(m => (m.innerText || '')).join('\n').slice(0, 800);
        return {
            mode: 'chat', recv: recvs.length, newRecv: newOnes.length, done,
            urls: imgs, videoBlocks, text,
        };
    }
    // --- branch B: inline (trang create-image, không có message list) ---
    clickUnloadedVideoBlocks(document);
    const current = [];
    if (kind === 'video') {
        current.push(...collectVideos(document));
    } else {
        document.querySelectorAll(sel.result_image).forEach(i => {
            const s = i.currentSrc || i.src || '';
            if (s && !s.startsWith('data:')) current.push(s);
        });
    }
    const newMedia = current.filter(u => mediaBefore.indexOf(u) === -1);
    return {
        mode: 'inline', newRecv: 1, done: newMedia.length > 0,
        urls: newMedia,
        text: (document.body.innerText || '').slice(0, 800),
    };
}"""


# Thu thập URL ảnh GỐC (full-size) từ image viewer. Preview trong chat dùng
# template `downsize_watermark` (nhỏ + watermark to); bản gốc do viewer lazy-load
# khi click ảnh (template `image_pre_watermark`, vd 1773x2364). Match theo base
# path (URL trước '~tplv') của các preview URL của job NÀY — không lấy nhầm
# ảnh job khác. Trả map base -> url gốc cho mọi ảnh đang có trên trang.
_FULLSIZE_JS = r"""({marker}) => {
    const out = {};
    document.querySelectorAll('img').forEach(i => {
        const s = i.currentSrc || i.src || '';
        if (!s || s.startsWith('data:') || s.indexOf(marker) === -1) return;
        const base = s.split('~tplv')[0];
        if (!out[base]) out[base] = s;   // ưu tiên bản đầu tiên (viewer)
    });
    return out;
}"""


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
        # refusal patterns: Cici từ chối kết quả (bản quyền / content policy).
        # Load từ config; fallback default nếu thiếu.
        self.refusal_patterns: list[str] = (
            cfg.get("messages", {}).get("refusal_patterns") or _DEFAULT_REFUSAL_PATTERNS
        )

    def _is_refusal_message(self, text: str) -> bool:
        """Cici từ chối hiển thị kết quả (bản quyền / content policy)?

        Match case-insensitive substring với refusal_patterns trong config.
        """
        if not text:
            return False
        low = text.lower()
        return any(p.lower() in low for p in self.refusal_patterns)

    # ---- connection lifecycle ------------------------------------------- #
    async def connect(self) -> None:
        """Start Playwright (chưa nối CDP — attach lazy khi cần). Idempotent."""
        if self._pw is None:
            self._pw = await async_playwright().start()

    async def _attach(self, timeout: float | None = None) -> None:
        """Connect/reconnect to the running Cici and locate the chat page.

        Retry với backoff. Nếu vượt `timeout` (giây) mà vẫn chưa nối được →
        raise ConnectionError để job FAILED nhanh thay vì treo cả queue.
        Mặc định dùng `cdp.connect_timeout` từ config.
        """
        await self.connect()
        cdp = self.cfg["cdp"]
        budget = timeout if timeout is not None else cdp.get("connect_timeout", 90)
        deadline = time.monotonic() + budget
        delay = cdp["reconnect_initial_delay"]
        mx = cdp["reconnect_max_delay"]
        while True:
            try:
                self._browser = await self._pw.chromium.connect_over_cdp(cdp["endpoint"])  # type: ignore[union-attr]
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
                if time.monotonic() >= deadline:
                    raise ConnectionError(
                        f"Cici CDP ({cdp['endpoint']}) không nối được sau {budget:.0f}s: {e}. "
                        "Kiểm tra Cici đang chạy với --remote-debugging-port=9222 (start_cici.bat)."
                    ) from e
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

    async def recover(self, timeout: float = 30.0) -> None:
        """Best-effort phục hồi UI sau lỗi/hard-deadline (reload trang chat).

        Nuốt mọi lỗi — mục tiêu chỉ là không để job KẾ TIẾP thừa trạng thái xấu.
        """
        try:
            if self._page is not None:
                try:
                    await self._page.title()
                except Exception:  # noqa: BLE001
                    await self._attach(timeout=timeout)
            else:
                await self._attach(timeout=timeout)
            if self._page is not None:
                await self._page.reload(wait_until="domcontentloaded", timeout=30000)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass

    # ---- low-level UI helpers ------------------------------------------- #
    async def _new_conversation(self) -> None:
        page = await self._ensure_page()
        # scope to main: there are two instances (sidebar + main panel)
        await page.get_by_role("main").locator(
            self.sel["new_conversation"]
        ).first.click()
        await asyncio.sleep(self.tm["ui_step_delay"])

    async def _select_skill(self, kind: str) -> None:
        """Legacy flow (build <= 147.0.7727.89): click skill button trong skill bar."""
        page = await self._ensure_page()
        sel = self.sel["skill_image"] if kind == "image" else self.sel["skill_video"]
        await page.locator(sel).click()
        await asyncio.sleep(self.tm["ui_step_delay"])

    async def _enter_creation_page(self, kind: str) -> None:
        """Entry chính (build 147.0.7727.149+): trang create-image ("Tác phẩm của AI").

        Fresh conversation không còn skill bar, nên vào thẳng trang tạo ảnh/video
        rồi chọn tab. Raise Playwright TimeoutError nếu tab không xuất hiện
        → caller fallback legacy flow.
        """
        page = await self._ensure_page()
        url = self.cfg["cdp"]["create_image_url"]
        if page.url != url:
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(self.tm["ui_step_delay"])
        tab = self.sel["creation_tab_image" if kind == "image" else "creation_tab_video"]
        tab_loc = page.locator('[data-testid="chat_input"]').locator(tab).first
        try:
            # SPA boot chậm sau idle lâu (tab render ~7s, có lúc >10s) —
            # chờ dài, và nếu vẫn mất thì goto lại một lần trước khi bỏ
            await tab_loc.wait_for(state="visible", timeout=15000)
        except Exception:  # noqa: BLE001
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(self.tm["ui_step_delay"])
            await tab_loc.wait_for(state="visible", timeout=15000)
        await tab_loc.click()
        await asyncio.sleep(self.tm.get("tab_delay", self.tm["ui_step_delay"]))

    def _resolve_model(self, kind: str, alias: str | None) -> dict:
        """Look up model option in cfg['models'][kind]. Alias None → default."""
        registry = self.cfg.get("models", {}).get(kind, {})
        if not alias:
            alias = registry.get("default")
        for opt in registry.get("options", []):
            if opt["alias"] == alias:
                return opt
        raise ValueError(
            f"Unknown model '{alias}' for kind '{kind}'. "
            f"Valid: {[o['alias'] for o in registry.get('options', [])]}"
        )

    def _resolve_option(self, kind: str, group: str, alias: str) -> dict:
        """Look up an option in cfg['options'][kind][group] (ratios/styles/durations)."""
        opts = self.cfg.get("options", {}).get(kind, {}).get(group, [])
        for o in opts:
            if o["alias"] == alias:
                return o
        raise ValueError(
            f"Unknown {group} '{alias}' for kind '{kind}'. "
            f"Valid: {[o['alias'] for o in opts]}"
        )

    @staticmethod
    def _has_text(select_text: str | list[str]):
        """select_text là string, hoặc list chuỗi đa ngôn ngữ (UI Cici có
        locale VI/EN) — list → regex khớp bất kỳ chuỗi nào."""
        if isinstance(select_text, list):
            return re.compile("|".join(re.escape(t) for t in select_text))
        return select_text

    async def _select_model(self, kind: str, alias: str | None) -> None:
        """Open model dropdown + click option. LUÔN chọn (kể cả default) —
        UI giữ model chọn lần cuối, không tin trạng thái mặc định."""
        opt = self._resolve_model(kind, alias)
        page = await self._ensure_page()
        await page.locator('[data-testid="chat_input"]').locator(
            self.sel["model_button"]
        ).first.click()
        await asyncio.sleep(self.tm.get("dropdown_delay", 0.6))
        # Radix menu render ở body level (popper wrapper), không nằm trong chat_input
        await page.locator(self.sel["model_option"],
                            has_text=self._has_text(opt["select_text"])).first.click()
        await asyncio.sleep(self.tm["ui_step_delay"])

    async def _select_dropdown(self, kind: str, group: str, alias: str,
                               button_sel: str) -> None:
        """Chọn 1 option trong dropdown (ratios/styles/durations)."""
        opt = self._resolve_option(kind, group, alias)
        page = await self._ensure_page()
        await page.locator('[data-testid="chat_input"]').locator(
            button_sel
        ).first.click()
        await asyncio.sleep(self.tm.get("dropdown_delay", 0.6))
        await page.locator(self.sel["model_option"],
                           has_text=self._has_text(opt["select_text"])).first.click()
        await asyncio.sleep(self.tm["ui_step_delay"])

    async def _upload_references(self, paths: list[str]) -> None:
        """Upload reference images (image + video tab).

        UI thay đổi theo version Cici — thử nhiều cách theo thứ tự:
          1. UI mới (>=147.0.7727.149, chat skill mode): mở modal Templates
             ("Templates"/"Mẫu") → set_input_files vào input ẩn → đóng modal.
          2. Nút "Reference Image" → native filechooser → set_files.
             (Trên trang create-image build .149, đây là đường chính — đã verify
             nút mở native chooser.)
        """
        if not paths:
            return
        # validate paths exist trước (fail fast, đừng để Cici lỗi mù)
        missing = [p for p in paths if not Path(p).exists()]
        if missing:
            raise FileNotFoundError(f"Reference files not found: {missing}")
        page = await self._ensure_page()
        main = page.get_by_role("main")

        # --- approach 1: Templates modal + hidden upload input ---
        tpl_texts = self.sel.get("templates_button_texts", ["Templates", "Mẫu"])
        for txt in tpl_texts:
            try:
                tpl = main.locator('button', has_text=txt).first
                await tpl.click(timeout=5000)
                await asyncio.sleep(self.tm.get("modal_open_delay", 1.5))
                fi = page.locator('input[type="file"]').first
                await fi.set_input_files(paths, timeout=5000)
                await asyncio.sleep(self.tm.get("modal_upload_delay", 2.0))
                # đóng modal để quay lại input chính
                await page.keyboard.press("Escape")
                await asyncio.sleep(self.tm.get("modal_close_delay", 1.0))
                log.info("Uploaded %d reference(s) via Templates modal (%r)", len(paths), txt)
                return
            except Exception as e:  # noqa: BLE001
                log.info("Templates-modal upload (%r) failed (%s); trying next", txt, e)

        # --- approach 2: Reference Image button + filechooser ---
        async with page.expect_file_chooser(timeout=5000) as fc_info:
            await page.locator(self.sel["ref_button"]).first.click()
        chooser = await fc_info.value
        await chooser.set_files(paths)
        await asyncio.sleep(max(self.tm["ui_step_delay"], 1.5))
        log.info("Uploaded %d reference(s) via filechooser", len(paths))

    async def _type_prompt(self, prompt: str) -> None:
        """In skill mode the input is a contenteditable TiPTap; fallback to textarea."""
        page = await self._ensure_page()
        inp = page.locator('[data-testid="chat_input"]').first
        editor = inp.locator(self.sel["editor_prose"]).last
        try:
            await editor.wait_for(state="visible", timeout=8000)
            await editor.click()
            await asyncio.sleep(self.tm.get("editor_focus_delay", 0.3))
            # xoá nội dung còn sót (job fail trước có thể để lại prompt dở),
            # rồi chèn nguyên khối qua một lệnh CDP. Type() từng ký tự với
            # delay 8ms quá chậm và dễ vỡ với prompt dài: ProseMirror
            # re-render giữa chừng làm action timeout.
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await page.keyboard.insert_text(prompt)
        except Exception as e:  # noqa: BLE001 — fallback to textarea (build cũ)
            log.warning("TiPTap prompt insert failed (%s); trying textarea fallback", e)
            ta = inp.locator(self.sel["chat_textarea"]).last
            await ta.wait_for(state="visible", timeout=10000)
            await ta.fill(prompt)

    async def _send(self) -> tuple[int, list[str]]:
        """Click send. Trả về (số bot messages TRƯỚC khi gửi, snapshot media URL
        trên trang trước khi gửi) để _wait_result chờ đúng kết quả job này (race-safe)."""
        page = await self._ensure_page()
        snap = await page.evaluate(_SNAPSHOT_JS, {"sel": self.sel})
        await page.locator('[data-testid="chat_input"]').locator(
            self.sel["send_button"]
        ).first.click()
        return int(snap.get("before", 0)), list(snap.get("media", []))

    async def _wait_result(self, timeout: float, kind: str = "image",
                           bot_count_before: int = 0,
                           media_before: list[str] | None = None) -> list[str]:
        """Poll result; return list of generated media URLs when done.

        Race-safe: branch chat chỉ nhìn bot messages từ index `bot_count_before`
        trở đi (job hiện tại sinh ra), KHÔNG phải "message mới nhất". Branch inline
        diff media với snapshot trước send. Video: <video> lazy-init khi click
        block → poll click trước, poll sau đọc src.

        Raises QuotaExhausted nếu text chứa báo hết quota hằng ngày.
        """
        page = await self._ensure_page()
        deadline = time.time() + timeout
        interval = self.tm["poll_interval"]
        media_before = media_before or []
        last_url_count = -1
        last_urls: list[str] | None = None
        stable_polls = 0
        while time.time() < deadline:
            res = await page.evaluate(
                _POLL_RESULT_JS,
                {"sel": self.sel, "before": bot_count_before, "mediaBefore": media_before, "kind": kind},
            )
            # detect quota-exhausted message BEFORE treating as success/timeout
            if _quota and res.get("text") and _quota.is_exhausted_message(res["text"]):
                raise QuotaExhausted(res["text"], kind)
            # detect content/copyright refusal — Cici gen xong nhưng chặn output.
            # Fail nhanh thay vì spin tới timeout (bảo vệ quota + thời gian).
            if res.get("text") and self._is_refusal_message(res["text"]):
                raise ContentBlocked(res["text"], kind)
            if res.get("mode") == "chat":
                # phải có ít nhất 1 bot message mới (job này đã được Cici nhận)
                if res.get("newRecv", 0) > 0 and res["urls"]:
                    if res["done"]:
                        return res["urls"]
                    # nếu chưa done nhưng số url tăng (Cici đang sinh thêm)
                    if len(res["urls"]) > last_url_count:
                        last_url_count = len(res["urls"])
            else:
                # inline: media xuất hiện khi gen xong; chờ ổn định 2 poll liên tiếp
                if res["urls"]:
                    if res["urls"] == last_urls:
                        stable_polls += 1
                        if stable_polls >= 2:
                            return res["urls"]
                    else:
                        last_urls = res["urls"]
                        stable_polls = 0
            await asyncio.sleep(interval)
        raise TimeoutError(f"No result within {timeout}s")

    async def _upgrade_to_fullsize(self, preview_urls: list[str],
                                   bot_count_before: int = 0) -> list[str]:
        """Đổi preview URL (downsize_watermark, ~288px) lấy ảnh GỐC full-size.

        Cơ chế (verify build 147.0.7727.149): click TỪNG ảnh kết quả → image
        viewer lazy-load bản `image_pre_watermark` (full-res, vd 1773x2364) cho
        ảnh đó → Escape đóng → ảnh kế. Viewer chỉ render 1 img full-size trong
        DOM (arrow keys không điều hướng) nên phải lặp qua từng box. Network
        listener bắt thêm các URL viewer prefetch. Match theo base path (URL
        trước '~tplv') của preview URL — không lấy nhầm ảnh job khác.

        Fail-safe: mọi bước bọc try/except — nếu viewer không mở / marker đổi /
        timeout thì trả lại preview URLs (kết quả cũ vẫn dùng được).
        """
        marker = self.sel.get("fullsize_image_marker", "")
        if not marker or not preview_urls:
            return preview_urls
        bases = {u.split("~tplv")[0]: u for u in preview_urls if "~tplv" in u}
        if not bases:
            return preview_urls
        page = await self._ensure_page()
        got: dict[str, str] = {}

        def _on_request(req) -> None:
            u = req.url
            if marker in u:
                base = u.split("~tplv")[0]
                if base in bases:
                    got.setdefault(base, u)

        try:
            page.on("request", _on_request)
            n_boxes = await page.evaluate(
                """([sel, before]) => {
                    const ml = document.querySelector(sel.message_list);
                    let scope = ml
                        ? Array.from(ml.querySelectorAll(sel.bot_message)).slice(before)[-1]
                        : document;
                    if (!scope || !scope.querySelectorAll) scope = document;
                    return scope.querySelectorAll(sel.result_image).length;
                }""",
                [self.sel, bot_count_before],
            )
            if not n_boxes:
                return preview_urls
            overall = time.time() + self.tm.get("fullsize_wait", 30)
            each = self.tm.get("fullsize_each_wait", 5)
            for i in range(n_boxes):
                if all(b in got for b in bases) or time.time() > overall:
                    break
                opened = await page.evaluate(
                    """([sel, before, i]) => {
                        const ml = document.querySelector(sel.message_list);
                        let scope = ml
                            ? Array.from(ml.querySelectorAll(sel.bot_message)).slice(before)[-1]
                            : document;
                        if (!scope || !scope.querySelectorAll) scope = document;
                        const boxes = scope.querySelectorAll(sel.result_image);
                        if (boxes[i]) { boxes[i].click(); return true; }
                        return false;
                    }""",
                    [self.sel, bot_count_before, i],
                )
                if not opened:
                    break
                # chờ bản full-size của ảnh này xuất hiện (DOM hoặc network)
                sub_deadline = time.time() + each
                before_count = len(got)
                while time.time() < sub_deadline:
                    for base, u in (await page.evaluate(
                            _FULLSIZE_JS, {"marker": marker})).items():
                        if base in bases:
                            got.setdefault(base, u)
                    if len(got) > before_count or all(b in got for b in bases):
                        break
                    await asyncio.sleep(0.4)
                await page.keyboard.press("Escape")
                await asyncio.sleep(self.tm.get("viewer_close_delay", 0.6))
            if not got:
                return preview_urls
            out = []
            for base, preview in bases.items():
                out.append(got.get(base, preview))
            upgraded = sum(1 for b in bases if b in got)
            log.info("Full-size upgrade: %d/%d ảnh gốc thu được", upgraded, len(bases))
            return out
        except Exception as e:  # noqa: BLE001 — upgrade là best-effort
            log.warning("Full-size upgrade failed (%s) — dùng preview URLs", e)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return preview_urls
        finally:
            try:
                page.remove_listener("request", _on_request)
            except Exception:  # noqa: BLE001
                pass

    # ---- high-level job execution --------------------------------------- #
    async def execute(self, job: Job) -> dict[str, Any]:
        async with self._lock:
            timeout = (
                self.tm["image_timeout"] if job.kind == "image" else self.tm["video_timeout"]
            )
            try:
                # Entry chính: trang create-image (build .149+). Fallback legacy
                # skill flow cho build cũ (skill bar trong conversation).
                try:
                    await self._enter_creation_page(job.kind)
                except Exception as e:  # noqa: BLE001
                    log.info("create-image entry failed (%s); falling back to legacy skill flow", e)
                    await self._new_conversation()
                    await self._select_skill(job.kind)
                await self._select_model(job.kind, job.model)
                if job.ratio:
                    await self._select_dropdown(job.kind, "ratios", job.ratio, self.sel["ratio_button"])
                if job.kind == "image" and job.style:
                    await self._select_dropdown(job.kind, "styles", job.style, self.sel["style_button"])
                if job.kind == "video" and job.duration:
                    await self._select_dropdown(job.kind, "durations", job.duration, self.sel["duration_button"])
                if job.references:
                    await self._upload_references(job.references)
                await self._type_prompt(job.prompt)
                bot_count_before, media_before = await self._send()
                urls = await self._wait_result(
                    timeout, kind=job.kind,
                    bot_count_before=bot_count_before, media_before=media_before,
                )
                # ảnh: nâng lên bản gốc full-size (viewer lazy-load) — best-effort
                if job.kind == "image":
                    urls = await self._upgrade_to_fullsize(urls, bot_count_before)
                # success → record quota (theo account label nếu có)
                if _quota:
                    state = _quota.load_account(job.account)
                    _quota.record_success(state, job.kind)
                    _quota.save_account(state, job.account)
                result: dict[str, Any] = {
                    "status": "COMPLETED",
                    "result_urls": urls,
                    "kind": job.kind,
                    "model": job.model or self.cfg["models"][job.kind]["default"],
                }
                for k in ("ratio", "style", "duration"):
                    if getattr(job, k):
                        result[k] = getattr(job, k)
                return result
            except QuotaExhausted as e:
                # quota hết → record limit-hit (auto-learn threshold, theo account)
                if _quota:
                    state = _quota.load_account(job.account)
                    thr = _quota.record_limit_hit(state, job.kind)
                    _quota.save_account(state, job.account)
                    log.warning("Job %s QUOTA EXHAUSTED (kind=%s, learned threshold=%s)",
                                job.job_id, job.kind, thr)
                return {
                    "status": "QUOTA_EXHAUSTED",
                    "kind": job.kind,
                    "message": e.raw_message,
                    "quota": _quota.snapshot(_quota.load_account(job.account), job.kind) if _quota else None,
                }
            except ContentBlocked as e:
                # Cici gen xong nhưng từ chối hiển thị kết quả (bản quyền / policy).
                # Không retry blind — phải đổi nội dung tham chiếu / prompt.
                log.warning("Job %s CONTENT BLOCKED (kind=%s): %s",
                            job.job_id, job.kind, e.raw_message[:160])
                return {
                    "status": "CONTENT_BLOCKED",
                    "kind": job.kind,
                    "message": e.raw_message,
                }
            except Exception as e:  # noqa: BLE001
                log.error("Job %s failed: %s", job.job_id, e)
                # clear zombie state so the next job isn't poisoned
                await self.recover()
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
    log.info("Worker starting; Playwright up (CDP attach lazily ở job đầu tiên).")
    await driver.connect()
    log.info("Worker ready, waiting for jobs.")
    while True:
        job: Job = await queue.get()
        store.set(job.job_id, status="PROCESSING", started_at=time.time())
        log.info("Processing job %s (%s)", job.job_id, job.kind)
        # Hard deadline = gen timeout + margin cho UI steps/upload. Bảo vệ queue
        # khi driver treo ngoài _wait_result (CDP retry, page.evaluate hang…).
        tm = cfg.get("timing", {})
        gen_to = tm.get(f"{job.kind}_timeout", 300 if job.kind == "image" else 600)
        margin = tm.get("hard_deadline_margin", 180)
        budget = gen_to + margin
        try:
            result = await asyncio.wait_for(driver.execute(job), timeout=budget)
            store.set(job.job_id, finished_at=time.time(), **result)
        except asyncio.TimeoutError:
            log.error("Job %s vượt hard deadline %.0fs — recover UI, đánh FAILED", job.job_id, budget)
            if hasattr(driver, "recover"):
                await driver.recover()
            store.set(
                job.job_id,
                status="FAILED",
                error=(f"Job vượt hard deadline {budget:.0f}s (gen {gen_to:.0f}s + margin {margin:.0f}s) — "
                       "Cici có thể treo hoặc CDP mất. Thử lại job; nếu lặp lại, restart Cici (start_cici.bat)."),
                finished_at=time.time(),
            )
        except Exception as e:  # noqa: BLE001  never kill the loop
            log.exception("Unhandled worker error on job %s", job.job_id)
            store.set(job.job_id, status="FAILED", error=str(e), finished_at=time.time())
        finally:
            queue.task_done()
