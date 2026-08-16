"""HTTP client tới cici-api core server + URL expiry parser.

Pure logic, không import click/rich — tách để test dễ.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx

DEFAULT_BASE = os.environ.get("CICI_API", "http://127.0.0.1:8000")

# Exit codes (chuẩn hoá để AI agent phân biệt lỗi)
EXIT_OK = 0
EXIT_FAILED = 1      # job COMPLETED với FAILED, hoặc lỗi logic
EXIT_TIMEOUT = 2     # gen không xong trong timeout
EXIT_PREFLIGHT = 3   # core server / Cici chưa chạy
EXIT_QUOTA = 4       # quota hằng ngày đã cạn (khác hẳn lỗi tạm thời — đừng retry ngay)


class CiciUnreachable(Exception):
    """Core server không trả lời."""


def health(base: str = DEFAULT_BASE, timeout: float = 5.0) -> dict:
    """GET /api/health. Raise CiciUnreachable nếu không nối được."""
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{base}/api/health")
        if r.status_code != 200:
            raise CiciUnreachable(f"health returned HTTP {r.status_code}")
        return r.json()
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        raise CiciUnreachable(str(e)) from e


def status(job_id: str, base: str = DEFAULT_BASE, timeout: float = 10.0) -> dict:
    """GET /api/status/{job_id} — poll một lần."""
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{base}/api/status/{job_id}")
    if r.status_code == 404:
        raise KeyError(job_id)
    r.raise_for_status()
    return r.json()


def models(base: str = DEFAULT_BASE, timeout: float = 10.0,
           provider: str = "cici") -> dict:
    """GET /api/models — registry {image: {...}, video: {...}} của provider."""
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{base}/api/models",
                      params={"provider": provider} if provider != "cici" else None)
        if r.status_code != 200:
            raise CiciUnreachable(f"models returned HTTP {r.status_code}")
        return r.json()
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        raise CiciUnreachable(str(e)) from e


def generate(prompt: str, kind: str, base: str = DEFAULT_BASE, timeout: float = 10.0,
             model: str | None = None, references: list[str] | None = None,
             ratio: str | None = None, style: str | None = None,
             duration: str | None = None, account: str | None = None,
             provider: str = "cici") -> dict:
    """POST /api/generate -> trả response dict ngay (server enqueue, không block).

    Dict gồm job_id + timeout_s (server-config gen timeout cho kind — CLI dùng
    thay vì hardcode local). Raise ValueError nếu 422, QuotaExhausted nếu 429.
    account = nhãn tách quota local (user TỰ đổi account trong app Cici).
    provider = "cici" (mặc định) hoặc "doubao" — app/CDP endpoint + registry riêng.
    """
    payload = {"prompt": prompt, "type": kind}
    if model:
        payload["model"] = model
    if references:
        payload["references"] = references
    if ratio:
        payload["ratio"] = ratio
    if style:
        payload["style"] = style
    if duration:
        payload["duration"] = duration
    if account:
        payload["account"] = account
    if provider and provider != "cici":
        payload["provider"] = provider
    with httpx.Client(timeout=timeout) as c:
        r = c.post(f"{base}/api/generate", json=payload)
    if r.status_code == 422:
        try:
            detail = r.json().get("detail", "invalid request")
        except Exception:
            detail = "invalid request"
        if isinstance(detail, list):
            # FastAPI validate schema → detail là list lỗi Pydantic — nén lại
            # thành string đọc được thay vì dump list repr nguyên khối
            detail = "; ".join(
                f"{'.'.join(str(x) for x in d.get('loc', [])[1:]) or 'body'}: "
                f"{d.get('msg', '')}"
                for d in detail if isinstance(d, dict)
            ) or "invalid request"
        raise ValueError(detail)
    if r.status_code == 429:
        # server refuse vì local quota estimate = 0
        try:
            detail = r.json().get("detail", {})
        except Exception:
            detail = {"message": r.text}
        raise QuotaExhausted(detail)
    r.raise_for_status()
    return r.json()


class QuotaExhausted(Exception):
    """Server refuse enqueue vì local quota estimate = 0."""
    def __init__(self, detail: dict):
        self.detail = detail
        super().__init__(detail.get("message", "quota exhausted"))


# Trạng thái terminal của job — wait_status dừng ngay khi thấy (đủ sớm, đủ
# đúng): QUOTA_EXHAUSTED/CONTENT_BLOCKED cũng là kết quả cuối, không poll tiếp.
TERMINAL_STATUSES = ("COMPLETED", "FAILED", "QUOTA_EXHAUSTED", "CONTENT_BLOCKED")


def quota(kind: str | None = None, account: str | None = None,
          base: str = DEFAULT_BASE, timeout: float = 10.0,
          provider: str = "cici") -> dict:
    """GET /api/quota — snapshot rolling 24h count + threshold.

    ?kind=image/video lọc theo loại; ?account=<nhãn> quota riêng từng account;
    ?provider= đọc state quota của provider đó (mặc định cici).
    """
    params = {}
    if kind:
        params["kind"] = kind
    if account:
        params["account"] = account
    if provider and provider != "cici":
        params["provider"] = provider
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{base}/api/quota", params=params)
    r.raise_for_status()
    return r.json()


def wait_status(
    job_id: str,
    timeout: float = 320.0,
    poll_interval: float = 4.0,
    base: str = DEFAULT_BASE,
    on_tick=None,
    status_fn=None,
    poll_max_interval: float = 15.0,
) -> dict:
    """Poll status tới khi COMPLETED/FAILED hoặc hết timeout.

    Queue-aware: thời gian đứng ở PENDING (chờ hàng đợi) KHÔNG tính vào
    `timeout` — chỉ thời gian PROCESSING bị giới hạn bởi `timeout`. Tránh
    tình huống nhiều agent gọi đồng thời: job xếp sau bị TIMEOUT oan dù server
    vẫn đang xử lý. Tổng thời gian chờ PENDING bị giới hạn bởi
    `timeout * (queue_ahead ban đầu + 1)` để không treo vô hạn khi server
    không bao giờ xử lý job.

    on_tick(status_dict) — callback mỗi lần poll (cho CLI in progress).
    status_fn — injectable cho test (mặc định gọi status() qua HTTP).

    Dừng ngay khi job đạt trạng thái terminal: COMPLETED / FAILED /
    QUOTA_EXHAUSTED / CONTENT_BLOCKED (2 trạng thái sau là kết quả cuối —
    trả về ngay để CLI báo đúng exit code 4/1 thay vì chờ hết timeout).

    Adaptive backoff: khi job đứng ở PENDING quá lâu (queue rảnh → server bận
    việc khác), sleep sẽ tăng dần `poll_interval * (1 + polls * 0.3)`, cap ở
    `poll_max_interval`. Khi job chuyển sang PROCESSING trở lại, reset về
    `poll_interval` (cần poll sát để biết lúc xong). Giảm HTTP traffic ~70%
    cho user batch enqueue nhiều job.
    """
    fn = status_fn or (lambda jid: status(jid, base=base))
    first = fn(job_id)
    if on_tick:
        on_tick(first)
    if first.get("status") in TERMINAL_STATUSES:
        return first
    queue_ahead = first.get("queue_ahead", 0) or 0
    pending_cap = time.time() + timeout * (queue_ahead + 1)
    processing_start: float | None = None
    last: dict = first
    last_status: str = first.get("status", "")
    polls_in_status = 0  # số lần liên tiếp poll thấy cùng status
    while True:
        now = time.time()
        if now > pending_cap:
            raise TimeoutError(
                f"job {job_id} chưa bắt đầu xử lý sau khi chờ queue "
                f"(ahead={queue_ahead}; cuối: {last.get('status')})"
            )
        last = fn(job_id)
        if on_tick:
            on_tick(last)
        st = last.get("status")
        if st in TERMINAL_STATUSES:
            return last
        # Reset poll counter khi status đổi
        if st == last_status:
            polls_in_status += 1
        else:
            last_status = st
            polls_in_status = 0
        if st == "PROCESSING" and processing_start is None:
            processing_start = now
        if processing_start is not None and now - processing_start > timeout:
            raise TimeoutError(
                f"job {job_id} chưa xong sau {timeout:.0f}s PROCESSING "
                f"(cuối: {last.get('status')})"
            )
        # Adaptive sleep: PENDING/UNKNOWN → backoff; PROCESSING → giữ nguyên poll_interval
        if st == "PROCESSING":
            sleep_s = poll_interval
        else:
            sleep_s = min(poll_interval * (1.0 + polls_in_status * 0.3), poll_max_interval)
        time.sleep(sleep_s)


# --------------------------------------------------------------------------- #
# URL expiry parsing
# --------------------------------------------------------------------------- #
def parse_expiry(url: str) -> int | None:
    """Trích unix timestamp từ query param `x-expires` của CDN Cici.

    Trả về int (unix seconds) hoặc None nếu URL không có.
    """
    qs = parse_qs(urlparse(url).query)
    vals = qs.get("x-expires") or qs.get("X-Expires")
    if not vals:
        return None
    try:
        return int(vals[0])
    except (ValueError, IndexError):
        return None


def expiry_local(url: str) -> datetime | None:
    """Trả về datetime local của expiry, hoặc None."""
    ts = parse_expiry(url)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()


def seconds_until_expiry(url: str, now: float | None = None) -> float | None:
    """Số giây còn lại trước khi URL hết hạn, hoặc None."""
    ts = parse_expiry(url)
    if ts is None:
        return None
    return ts - (now or time.time())
