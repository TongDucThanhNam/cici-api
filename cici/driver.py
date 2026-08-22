"""Cici browser driver — wraps Playwright CDP connection to the running Cici app.

Single consumer. Hardened:
  - auto-reconnect to CDP if Cici restarted / port dropped
  - per-job timeout -> mark FAILED + reload page to clear zombie state
  - never crashes the worker loop (keeps draining the queue)

`Job`, `JobStore`, and `run_worker` remain re-exported here for compatibility;
their implementations live in `cici.jobs` and `cici.worker`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
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

from cici._interaction import (
    DEFAULT_CONFIRM_PATTERNS as _DEFAULT_CONFIRM_PATTERNS,
    DEFAULT_REFUSAL_PATTERNS as _DEFAULT_REFUSAL_PATTERNS,
    InteractionPolicy,
)
from cici.catalog import ConfigCatalog
from cici.jobs import Job, JobStore

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


class NeedsInteraction(Exception):
    """Bot hỏi xác nhận lặp lại sau N lần auto-reply "confirm".

    Prompt dạng parameter sheet đôi khi khiến Dola hỏi "Reply 'confirm' and
    I'll generate" thay vì gen ngay. Driver tự reply "confirm" tối đa
    messages.auto_confirm_max lần; vẫn hỏi lại → fail nhanh (không spin tới
    timeout, không block queue) + reload page cho job kế.
    """
    def __init__(self, message: str, kind: str, attempts: int):
        super().__init__(message)
        self.kind = kind
        self.raw_message = message
        self.attempts = attempts


log = logging.getLogger("cici.driver")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


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
        const seen = new Set();
        const push = (s) => {
            if (s && !s.startsWith('data:') && !s.startsWith('blob:') && !seen.has(s)) {
                seen.add(s);
                out.push(s);
            }
        };
        root.querySelectorAll('video').forEach(v => push(v.currentSrc || v.src || ''));
        root.querySelectorAll('video source').forEach(v => push(v.src || ''));
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
            // Doubao: action bar KHÔNG xuất hiện trên video message → fallback
            // done theo text thông báo hoàn tất (sel.video_done_patterns,
            // case-insensitive substring). Contract giữ nguyên: done vẫn yêu
            // cầu videos.length > 0 (URL thật) bên dưới.
            const pats = sel.video_done_patterns || [];
            const lowText = text.toLowerCase();
            const textDone = pats.some(p => {
                try { return lowText.includes(String(p).toLowerCase()); } catch (e) { return false; }
            });
            return {
                mode: 'chat', recv: recvs.length, newRecv: newOnes.length,
                done: (done || textDone) && videos.length > 0,
                urls: videos, videoBlocks, text,
                lastText: (last.innerText || '').slice(0, 400),
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


def _parse_watermark_free_video(payload: Any) -> list[str]:
    """Trích URL video KHÔNG watermark từ response get_without_watermark.

    Shape (theo client bundle Cici build 147.0.7727.149, requestScene
    "remove_ai_watermark"):
      {"code": 0, "data": {"download_video": {"<vid>": {
          "download_url": str | list[str],      # URL sạch ưu tiên
          "video_model": [{"main_url"|"url": str}, ...]   # fallback
      }}}}

    Nhận cả raw text JSON lẫn dict đã parse. Trả list URL theo thứ tự; rỗng khi
    không có gì dùng được (account không được bật tính năng / shape đổi).
    """
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return []
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    download_video = data.get("download_video")
    if not isinstance(download_video, dict):
        return []
    out: list[str] = []
    for entry in download_video.values():
        if not isinstance(entry, dict):
            continue
        got: list[str] = []
        du = entry.get("download_url")
        if isinstance(du, str):
            got.append(du)
        elif isinstance(du, list):
            got.extend(u for u in du if isinstance(u, str))
        if not got:
            for vm in entry.get("video_model") or []:
                if isinstance(vm, dict):
                    u = vm.get("main_url") or vm.get("url")
                    if isinstance(u, str):
                        got.append(u)
        out.extend(got)
    return out


def _extract_video_resource_keys(video_urls: list[str]) -> tuple[list[str], list[str]]:
    """Suy ra (vids, uris) từ các URL video CDN của Cici/Doubao.

    Shape URL (verify build 147.0.7727.149):
      https://v16-dola.dola.com/<hash1>/<hash2>/video/tos/<region>/<bucket>/<key>/...
      vd bucket = tos-mya-ve-50851, key = oYixyBES31DD...

    API get_without_watermark nhận:
      vid = <key>          (verify live: code 0)
      uri = <bucket>/<key> (verify live: code 0)
    Trả (keys, bucket/key list) khử trùng, giữ thứ tự.
    """
    vids: list[str] = []
    uris: list[str] = []
    for u in video_urls:
        try:
            path = u.split("?", 1)[0].split("#", 1)[0]
        except Exception:  # noqa: BLE001 — URL rác thì bỏ qua
            continue
        if "/video/tos/" not in path:
            continue
        tail = path.split("/video/tos/", 1)[1].strip("/")
        parts = [p for p in tail.split("/") if p]
        # tail = <region>/<bucket>/<key> — cần ít nhất 3 phần sau "tos"
        if len(parts) < 3:
            continue
        bucket, key = parts[-2], parts[-1]
        if not key or key in vids:
            continue
        vids.append(key)
        uris.append(f"{bucket}/{key}")
    return vids, uris


# Gọi API get_without_watermark từ context trang chat (cookie phiên tự đính kèm).
# Trả body text thô để _parse_watermark_free_video xử lý.
_WATERMARK_FETCH_JS = r"""async ({host, payload}) => {
    const r = await fetch('https://' + host + '/creativity/resource/get_without_watermark', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)});
    return await r.text();
}"""


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
class CiciDriver:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.catalog = ConfigCatalog(cfg)
        self.sel = cfg["selectors"]
        self.tm = cfg["timing"]
        self._pw = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()  # serialize all UI ops
        # provider của connection hiện tại — đổi provider giữa 2 job thì phải
        # detach CDP cũ rồi attach lại endpoint mới (queue tuần tự nên an toàn)
        self._current_provider: str = "cici"
        # refusal patterns: Cici từ chối kết quả (bản quyền / content policy).
        # Load từ config; fallback default nếu thiếu.
        self.refusal_patterns: list[str] = (
            cfg.get("messages", {}).get("refusal_patterns") or _DEFAULT_REFUSAL_PATTERNS
        )
        # confirm-request patterns: bot hỏi xác nhận trước khi gen — driver tự
        # reply "confirm" (bounded) thay vì spin tới timeout.
        self.confirm_patterns: list[str] = (
            cfg.get("messages", {}).get("confirm_request_patterns") or _DEFAULT_CONFIRM_PATTERNS
        )
        self._interaction = InteractionPolicy(
            self.refusal_patterns,
            self.confirm_patterns,
        )
        self.auto_confirm_max: int = int(
            cfg.get("messages", {}).get("auto_confirm_max", 2) or 0)

    # ---- provider resolution -------------------------------------------- #
    def _cdp_for(self, provider: str) -> dict:
        """Section cdp cho provider: base + overlay cdp.providers.<name>."""
        cdp = dict(self.cfg.get("cdp", {}))
        overlay = (self.cfg.get("cdp", {}).get("providers") or {}).get(provider)
        if overlay:
            cdp.update(overlay)
        return cdp

    def _registry(self, section: str, provider: str) -> dict:
        """Backward-compatible facade over the shared provider catalog."""

        return self.catalog.section(section, provider)

    def _is_refusal_message(self, text: str) -> bool:
        """Cici từ chối hiển thị kết quả (bản quyền / content policy)?"""

        return self._interaction.is_refusal(text)

    def _is_confirm_request(self, text: str) -> bool:
        """Bot đang hỏi xác nhận ("Reply 'confirm'...", "reply "Generate"...")
        thay vì gen?

        Match case-insensitive substring với confirm_request_patterns trong
        config, HOẶC structural: text chứa ≥2 option chọn theo chữ cái kèm
        thời lượng (A. 5 seconds / B. 10 giây…) — Dola đôi khi hỏi "Which
        duration do you want?" mà không kèm lệnh "reply X". Chỉ dùng cho LAST
        bot message (lastText) — text gộp của nhiều message sẽ khớp vĩnh viễn
        sau khi đã reply.
        """
        return self._interaction.is_confirm_request(text)

    # Bot chỉ định token để user reply tiếp tục — token nằm trong cặp nháy
    # (straight/curly): Reply "confirm" / reply “Generate” / trả lời “Tạo”.
    _REPLY_TOKEN_RE = InteractionPolicy.REPLY_TOKEN_RE
    # Câu hỏi chọn theo chữ cái: "A. 5 seconds" / "B. 10 giây" / "C. 15秒"
    # / "A. 5s". Yêu cầu delimiter sau chữ cái để không khớp "a 5 seconds"
    # trong văn xuôi.
    _CHOICE_RE = InteractionPolicy.CHOICE_RE

    def _extract_reply_token(self, text: str) -> str | None:
        """Token bot bảo user reply ("Generate"/"confirm"/…) — None nếu không có."""
        return self._interaction.extract_reply_token(text)

    def _duration_choice(self, text: str, duration: str | None) -> str | None:
        """Bot hỏi chọn A/B/C theo số giây — trả chữ cái khớp duration alias
        ("10s" → "B"). Duration None / không khớp option → None."""
        return self._interaction.duration_choice(text, duration)

    def _is_choice_question(self, text: str) -> bool:
        """Text có ≥2 option chọn theo chữ cái kèm thời lượng (A. 5 seconds…)?

        Dola có khi hỏi "Which duration do you want?" mà không kèm lệnh
        "reply X" — danh sách A/B/C vẫn là câu hỏi chờ input, driver phải tự
        reply. Option đơn lẻ trong văn xuôi ("a 5-second version") không tính.
        """
        return self._interaction.is_choice_question(text)

    def _auto_reply_text(self, text: str, duration: str | None = None) -> str:
        """Nội dung tự reply khi bot hỏi xác nhận:
        1. Có options A/B/C theo giây + job đặt duration → trả chữ cái khớp
           (job -d 10s phải ra video 10s, không rơi về default 5s).
        2. Bot chỉ định token ("Generate"/"confirm") → trả token đó.
        3. Chỉ có danh sách A/B/C (không token) → option đầu (5s default).
        4. Fallback → "confirm".
        """
        return self._interaction.auto_reply_text(text, duration)

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
        cdp = self._cdp_for(self._current_provider)
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
                        f"{self._current_provider} CDP ({cdp['endpoint']}) không nối được "
                        f"sau {budget:.0f}s: {e}. Kiểm tra app đang chạy với đúng "
                        "--remote-debugging-port (xem providers trong config.yaml)."
                    ) from e
                log.warning("CDP connect failed (%s); retry in %ss", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, mx)

    async def _switch_provider(self, provider: str) -> None:
        """Đổi provider nếu khác connection hiện tại: detach CDP cũ (không kill
        app — connect_over_cdp close() chỉ ngắt kết nối), job sau attach mới."""
        if provider == self._current_provider:
            return
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001 — best-effort detach
                pass
        self._browser = None
        self._page = None
        self._current_provider = provider
        log.info("Switched provider -> %s (CDP %s)", provider,
                 self._cdp_for(provider).get("endpoint"))

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
        url = self._cdp_for(self._current_provider)["create_image_url"]
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
        """Look up model option trong registry của provider hiện tại.
        Alias None → default."""

        return self.catalog.resolve_model(kind, alias, self._current_provider)

    def _resolve_option(self, kind: str, group: str, alias: str) -> dict:
        """Look up an option in registry của provider hiện tại
        (cfg['options'][<kind>][<group>] theo provider)."""
        return self.catalog.resolve_option(kind, group, alias, self._current_provider)

    @staticmethod
    def _has_text(select_text: str | list[str], exact: bool = False):
        """select_text là string, hoặc list chuỗi đa ngôn ngữ (UI Cici có
        locale VI/EN/ZH) — list → regex khớp bất kỳ chuỗi nào.

        exact=True neo ^$ để dùng làm accessible-name (get_by_role name=...)
        — tránh "16:9" khớp nhầm nút toolbar "比例 16:9".
        """
        if isinstance(select_text, list):
            pat = "|".join(re.escape(t) for t in select_text)
            return re.compile(f"^(?:{pat})$") if exact else re.compile(pat)
        if exact:
            return re.compile(f"^{re.escape(select_text)}$")
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
        # Option click: Cici dùng Radix menuitem; Doubao ratio/duration mở
        # popover chứa BUTTON thường — auto-detect: không có menuitem thì click
        # button theo accessible-name exact (neo ^$ tránh trúng nút toolbar).
        opt_loc = page.locator(self.sel["model_option"],
                               has_text=self._has_text(opt["select_text"]))
        if await opt_loc.count() == 0:
            opt_loc = page.get_by_role(
                "button", name=self._has_text(opt["select_text"], exact=True))
        await opt_loc.first.click()
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
                           media_before: list[str] | None = None,
                           duration: str | None = None) -> list[str]:
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
        # sel cho poll: ghép thêm video_done_patterns (messages config) để
        # _POLL_RESULT_JS dùng làm done fallback cho video (Doubao).
        poll_sel = dict(self.sel)
        done_pats = self.cfg.get("messages", {}).get("video_done_patterns")
        if done_pats:
            poll_sel["video_done_patterns"] = done_pats
        last_url_count = -1
        last_urls: list[str] | None = None
        stable_polls = 0
        confirms_sent = 0
        while time.time() < deadline:
            res = await page.evaluate(
                _POLL_RESULT_JS,
                {"sel": poll_sel, "before": bot_count_before, "mediaBefore": media_before, "kind": kind},
            )
            # detect quota-exhausted message BEFORE treating as success/timeout
            if _quota and res.get("text") and _quota.is_exhausted_message(res["text"]):
                raise QuotaExhausted(res["text"], kind)
            # detect content/copyright refusal — Cici gen xong nhưng chặn output.
            # Fail nhanh thay vì spin tới timeout (bảo vệ quota + thời gian).
            if res.get("text") and self._is_refusal_message(res["text"]):
                raise ContentBlocked(res["text"], kind)
            # Bot hỏi xác nhận ("Reply 'confirm'...", "reply "Generate"...")
            # thay vì gen — phổ biến với prompt dạng parameter sheet. Tự reply
            # (bounded): chữ cái khớp duration nếu bot hỏi A/B/C, ngược lại
            # token bot chỉ định ("Generate"/"confirm"). Hỏi lại quá số lần →
            # fail nhanh, không spin tới timeout. Chỉ soi LAST bot message để
            # không khớp lại câu hỏi đã reply.
            last_text = res.get("lastText") or ""
            if (res.get("mode") == "chat" and res.get("newRecv", 0) > 0
                    and not res.get("urls")
                    and self._is_confirm_request(last_text)):
                if confirms_sent < self.auto_confirm_max:
                    confirms_sent += 1
                    reply = self._auto_reply_text(last_text, duration)
                    log.info("Bot hỏi xác nhận — tự reply %r (lần %d/%d)",
                             reply, confirms_sent, self.auto_confirm_max)
                    try:
                        await self._type_prompt(reply)
                        await self._send()
                    except Exception as e:  # noqa: BLE001 — reply fail thì skip
                        log.warning("Auto-confirm reply failed: %s", e)
                    await asyncio.sleep(interval)
                    continue
                raise NeedsInteraction(
                    last_text or res.get("text") or "", kind, confirms_sent)
            # Doubao (verify live 2026-08-22): xgplayer chỉ lazy-init khi HOVER
            # thật bằng input event — JS click trong evaluate không khởi động
            # được. Hover block video cuối mỗi khi chưa đọc được URL nào.
            if (kind == "video" and not res.get("urls")
                    and res.get("videoBlocks", 0) > 0):
                try:
                    await page.locator(self.sel["result_video"]).last.hover(timeout=2000)
                except Exception:  # noqa: BLE001 — hover best-effort
                    pass
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

    def _fullsize_marker(self) -> str:
        """Marker template URL bản gốc theo provider hiện tại
        (selectors.fullsize_markers.<provider> → fallback fullsize_image_marker)."""
        return (self.sel.get("fullsize_markers", {})
                .get(self._current_provider)
                or self.sel.get("fullsize_image_marker", ""))

    async def _upgrade_to_fullsize(self, preview_urls: list[str],
                                   bot_count_before: int = 0) -> list[str]:
        """Đổi preview URL (downsize_watermark, ~288px) lấy ảnh GỐC full-size.

        Cơ chế (verify build 147.0.7727.149): click TỪNG ảnh kết quả → image
        viewer lazy-load bản `image_pre_watermark` (full-res, vd 1773x2364) cho
        ảnh đó → Escape đóng → ảnh kế. Viewer chỉ render 1 img full-size trong
        DOM (arrow keys không điều hướng) nên phải lặp qua từng box. Network
        listener bắt thêm các URL viewer prefetch. Match theo base path (URL
        trước '~tplv') của preview URL — không lấy nhầm ảnh job khác.

        Doubao (provider doubao): click ảnh mở SIDE PANEL (không phải modal
        viewer); bản gốc `i_pre_wm` (vd 2848x1600) chỉ đi qua NETWORK — DOM img
        giữ preview URL — nên network listener là nguồn chính, DOM poll không
        ra gì (vô hại). Escape có thể không đóng panel — không sao: click ảnh
        kế vẫn hoạt động, và job sau goto lại trang create-image.

        Fail-safe: mọi bước bọc try/except — nếu viewer không mở / marker đổi /
        timeout thì trả lại preview URLs (kết quả cũ vẫn dùng được).
        """
        # marker bản full-size theo provider (cici: image_pre_watermark;
        # doubao: i_pre_wm — xem selectors.fullsize_markers trong config)
        marker = self._fullsize_marker()
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

    async def _capture_watermark_free_video(self, video_urls: list[str],
                                            bot_count_before: int = 0) -> list[str]:
        """Đổi URL video (kèm watermark) lấy URL KHÔNG watermark — best-effort.

        Gọi TRỰC TIẾP API sanctioned của ByteDance từ context trang chat
        (cookie phiên của app tự đính kèm):
          POST https://<host>/creativity/resource/get_without_watermark
          {"vid": ["<object-key>"]}   (hoặc {"uri": ["<bucket>/<object-key>"]})

        vid/uri suy ra từ chính URL video (path /video/tos/<...>/<bucket>/<key>/).
        Verify live 2026-08-22: đúng shape array thì server trả code 0; account
        không có entitlement → `without_watermark: false` (không có URL sạch)
        → giữ URL gốc, job vẫn COMPLETED.

        Host per provider: selectors.video_watermark_api_hosts.<provider>.
        Provider ngoài selectors.video_watermark_providers → bỏ qua.
        """
        if not video_urls:
            return video_urls
        providers = self.sel.get("video_watermark_providers") or ["cici"]
        if self._current_provider not in providers:
            return video_urls
        host = (self.sel.get("video_watermark_api_hosts") or {}).get(self._current_provider)
        if not host:
            return video_urls
        vids, uris = _extract_video_resource_keys(video_urls)
        if not vids and not uris:
            return video_urls
        budget = self.tm.get("video_watermark_wait", 30)
        page = await self._ensure_page()
        try:
            for payload in ({"vid": vids}, {"uri": uris}):
                if not payload.get("vid") and not payload.get("uri"):
                    continue
                try:
                    body = await asyncio.wait_for(
                        page.evaluate(_WATERMARK_FETCH_JS,
                                      {"host": host, "payload": payload}),
                        timeout=budget)
                except Exception as e:  # noqa: BLE001 — network/eval fail
                    log.warning("Watermark-free fetch failed (%s) — thử biến thể kế", e)
                    continue
                clean = _parse_watermark_free_video(body)
                if clean:
                    log.info("Watermark-free upgrade: %d/%d URL video sạch thu được",
                             len(clean), len(video_urls))
                    return clean
            log.info("Watermark-free upgrade: without_watermark=false — giữ URL gốc")
            return video_urls
        except Exception as e:  # noqa: BLE001 — capture là best-effort
            log.warning("Watermark-free capture failed (%s) — dùng URL gốc", e)
            return video_urls

    # ---- high-level job execution --------------------------------------- #
    async def execute(self, job: Job) -> dict[str, Any]:
        async with self._lock:
            timeout = (
                self.tm["image_timeout"] if job.kind == "image" else self.tm["video_timeout"]
            )
            # provider quyết định CDP endpoint + registry — đổi app thì detach cũ
            await self._switch_provider(job.provider)
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
                    duration=job.duration,
                )
                # ảnh: nâng lên bản gốc full-size (viewer lazy-load) — best-effort
                if job.kind == "image":
                    urls = await self._upgrade_to_fullsize(urls, bot_count_before)
                # video: capture URL không watermark qua flow download của app
                elif job.kind == "video":
                    urls = await self._capture_watermark_free_video(urls, bot_count_before)
                # success → record quota (theo account label + provider nếu có)
                if _quota:
                    state = _quota.load_account(job.account, provider=job.provider)
                    _quota.record_success(state, job.kind)
                    _quota.save_account(state, job.account, provider=job.provider)
                result: dict[str, Any] = {
                    "status": "COMPLETED",
                    "result_urls": urls,
                    "kind": job.kind,
                    "model": job.model or self._registry("models", job.provider)[job.kind]["default"],
                }
                for k in ("ratio", "style", "duration"):
                    if getattr(job, k):
                        result[k] = getattr(job, k)
                return result
            except QuotaExhausted as e:
                # quota hết → record limit-hit (auto-learn threshold, theo
                # account + provider)
                if _quota:
                    state = _quota.load_account(job.account, provider=job.provider)
                    thr = _quota.record_limit_hit(state, job.kind)
                    _quota.save_account(state, job.account, provider=job.provider)
                    log.warning("Job %s QUOTA EXHAUSTED (kind=%s, learned threshold=%s)",
                                job.job_id, job.kind, thr)
                return {
                    "status": "QUOTA_EXHAUSTED",
                    "kind": job.kind,
                    "message": e.raw_message,
                    "quota": _quota.snapshot(_quota.load_account(job.account, provider=job.provider), job.kind) if _quota else None,
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
            except NeedsInteraction as e:
                # Bot hỏi xác nhận lặp lại sau N lần auto-reply "confirm" — fail
                # nhanh + reload page để job kế không kẹt conversation cũ.
                log.warning("Job %s NEEDS INTERACTION (kind=%s, replies=%d): %s",
                            job.job_id, job.kind, e.attempts, e.raw_message[:160])
                await self.recover()
                return {
                    "status": "FAILED",
                    "kind": job.kind,
                    "error": (
                        f"Dola vẫn yêu cầu xác nhận sau {e.attempts} lần reply "
                        f"'confirm' — đổi prompt đơn giản hơn rồi thử lại"
                    ),
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
    """Compatibility facade; worker policy lives in :mod:`cici.worker`."""

    from cici.worker import run_worker as _run_worker

    await _run_worker(queue, store, cfg, driver_factory=CiciDriver)
