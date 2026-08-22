"""Job store persistence — write-ahead JSON file for crash recovery.

Tại sao cần:
  JobStore trong cici/jobs.py là in-memory dict; server restart mất hết job. Khi
  Cici/CDP chết giữa job đang xử lý, user phải tự re-enqueue — khó chịu cho
  batch/agent user. File này cung cấp load/save đơn giản, fail-open y hệt
  _quota.py: corrupt file → empty dict, OSError/JSONDecodeError bị nuốt.

Schema (file JSON):
  {
    "<job_id>": {
      "status": "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED" | "QUOTA_EXHAUSTED" | "CONTENT_BLOCKED",
      "kind": "image" | "video",
      "model": "<alias>" | null,
      "prompt": "...",
      "ratio": "..." | null,
      "style": "..." | null,
      "duration": "..." | null,
      "references": [...],
      "result_urls": [...],
      "error": "..." | null,
      "message": "..." | null,
      "seq": <int>,
      "created_at": <float>,
      "started_at": <float> | null,
      "finished_at": <float> | null,
      "queue_ahead": <int>,        # chỉ thêm bởi /api/status; không persist (per-call)
      "queue_size": <int>          # chỉ thêm bởi /api/status; không persist (per-call)
    },
    ...
  }

Server restore logic (xem cici/server.py lifespan):
  - Load file; nếu không có hoặc corrupt → STORE rỗng.
  - Bất kỳ job nào ở PENDING/PROCESSING khi restore → mark FAILED với error
    "server restarted mid-job" (an toàn — job có thể đã gen xong mà ta không
    biết; agent retry sẽ enqueue lại).

Pure logic, không import FastAPI/driver — dễ test.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from cici.jobs import (
    IN_FLIGHT_STATUSES as _IN_FLIGHT_STATUSES,
    TERMINAL_STATUSES as _TERMINAL_STATUSES,
)

DEFAULT_JOBS_PATH = Path.home() / ".cici" / "jobs.json"

# Retention: job terminal cũ hơn khoảng này bị bỏ khỏi file khi save — jobs.json
# chứa đầy đủ prompt + result URL nên không để mọc vô hạn theo thời gian sử dụng.
DEFAULT_RETENTION_SECONDS = 7 * 24 * 3600

# Trường per-call mà /api/status tự thêm vào (không persist).
EPHEMERAL_KEYS = ("queue_ahead", "queue_size")


def load_jobs(path: Path = DEFAULT_JOBS_PATH) -> dict[str, dict[str, Any]]:
    """Đọc jobs.json. Trả dict rỗng nếu file không tồn tại hoặc corrupt (fail-open)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, KeyError):
        # FileNotFoundError/PermissionError/IsADirectoryError ⊂ OSError;
        # JSONDecodeError ⊂ ValueError.
        return {}

    if not isinstance(data, dict):
        return {}

    # Sanitize: chỉ giữ entry value là dict, các key bắt buộc nếu thiếu → defaults.
    out: dict[str, dict[str, Any]] = {}
    for jid, entry in data.items():
        if not isinstance(jid, str) or not isinstance(entry, dict):
            continue
        clean = {k: v for k, v in entry.items() if k not in EPHEMERAL_KEYS}
        # Defaults cho field cốt lõi (không raise nếu thiếu — file cũ/partial vẫn load).
        clean.setdefault("status", "UNKNOWN")
        clean.setdefault("kind", "image")
        clean.setdefault("created_at", 0.0)
        out[jid] = clean
    return out


def prune_jobs(jobs: dict[str, dict[str, Any]],
               keep_seconds: float = DEFAULT_RETENTION_SECONDS,
               now: float | None = None) -> dict[str, dict[str, Any]]:
    """Trả BẢN SAO chỉ còn các job nên giữ: job terminal cũ hơn `keep_seconds`
    tính từ finished_at (fallback created_at) bị bỏ.

    Không mutate `jobs` (store trong RAM giữ nguyên history tới lúc restart).
    Fail-safe: in-flight, status lạ, hoặc thiếu mốc thời gian hợp lệ → luôn giữ.
    """
    if now is None:
        now = time.time()
    if keep_seconds is None or keep_seconds < 0:
        return dict(jobs)
    cutoff = now - keep_seconds
    out: dict[str, dict[str, Any]] = {}
    for jid, entry in jobs.items():
        if entry.get("status") in _TERMINAL_STATUSES:
            ts = entry.get("finished_at")
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                ts = entry.get("created_at")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts < cutoff:
                continue
        out[jid] = entry
    return out


def save_jobs(jobs: dict[str, dict[str, Any]], path: Path = DEFAULT_JOBS_PATH,
              keep_seconds: float | None = DEFAULT_RETENTION_SECONDS) -> None:
    """Ghi jobs ra JSON. mkdir(parents=True, exist_ok=True). Bỏ qua EPHEMERAL_KEYS.

    Job terminal cũ hơn `keep_seconds` bị prune khỏi payload (retention — giữ
    file nhỏ); truyền keep_seconds=None để tắt. Store trong RAM không bị mutate.

    Không atomic (không dùng tempfile + os.replace) — repo không có pattern đó.
    Crash giữa write có thể làm file partial; load_jobs() sẽ swallow → mất jobs
    in-flight, fail-open. Acceptable cho use case này.
    """
    if not isinstance(jobs, dict):
        return
    # Strip ephemeral per-call fields trước khi ghi + prune theo retention.
    payload = prune_jobs(
        {
            jid: {k: v for k, v in entry.items() if k not in EPHEMERAL_KEYS}
            for jid, entry in jobs.items()
            if isinstance(jid, str) and isinstance(entry, dict)
        },
        keep_seconds=keep_seconds,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # read-only home / disk full — bỏ qua, server vẫn chạy được (chỉ mất persist).
        pass


def reconcile_on_boot(jobs: dict[str, dict[str, Any]], now: float | None = None) -> int:
    """Đánh dấu job in-flight (PENDING/PROCESSING) thành FAILED khi server boot lại.

    Trả về số job đã thu dọn. Mutates `jobs` in-place (không save — caller quyết
    định khi nào ghi disk sau khi reconcile xong cùng với các update khác).
    """
    if now is None:
        now = time.time()
    reconciled = 0
    for entry in jobs.values():
        if entry.get("status") in _IN_FLIGHT_STATUSES:
            entry["status"] = "FAILED"
            entry["error"] = "server restarted mid-job (job state recovered from disk)"
            entry["finished_at"] = now
            reconciled += 1
    return reconciled


def merge_into_store(store_data: dict[str, dict[str, Any]],
                     loaded: dict[str, dict[str, Any]]) -> int:
    """Merge loaded jobs vào store_data hiện tại. Job mới (loaded) thắng job cũ
    (đang trong RAM) vì job trên disk được persist gần nhất → state chính xác hơn.

    Returns số entry đã thêm/cập nhật.
    """
    count = 0
    for jid, entry in loaded.items():
        store_data[jid] = entry
        count += 1
    return count
