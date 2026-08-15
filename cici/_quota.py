"""Quota tracker — rolling 24h local count + auto-learn threshold.

Cici không tiết lộ quota còn lại, nên tool tự track ở local. Logic:

  - Mỗi gen thành công → ghi timestamp vào history (per kind: image/video).
  - Mỗi lần hit "đã đạt giới hạn" → ghi timestamp + count tại lúc đó = threshold học được.
  - Rolling 24h: chỉ đếm timestamp trong 24h gần nhất.
  - Auto-learn: nếu current_count (24h) khi hit limit == N → threshold[kind] = N.
    Từ đó cảnh báo khi count sắp tới N, và từ chối gen để khỏi tốn thời gian chờ fail.

State lưu ở ~/.cici/quota.json (cross-platform qua Path.home()).

Pure logic, không import CLI/HTTP — dễ test.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

WINDOW_SECONDS = 24 * 3600  # rolling window
DEFAULT_STATE_PATH = Path.home() / ".cici" / "quota.json"

# Patterns báo quota exhausted (match trên text trong bot message, case-insensitive).
# Thêm pattern khi thấy Cici đổi wording.
QUOTA_EXHAUSTED_PATTERNS = [
    "đã đạt đến giới hạn tạo hình ảnh",
    "đã đạt giới hạn tạo video",
    "đã đạt giới hạn",
    "đạt đến giới hạn",
    "reached your daily limit",
    "daily limit reached",
    "try again tomorrow",
    "thử lại vào ngày mai",
    "quay lại để tạo thêm vào ngày mai",
]


@dataclass
class QuotaState:
    """Trạng thái quota, serialize sang JSON."""
    # history per kind: list of {"t": unix_ts} cho mỗi gen thành công
    history: dict[str, list[float]] = field(default_factory=lambda: {"image": [], "video": []})
    # threshold học được per kind: số gen tối đa trước khi hit limit (None = chưa học)
    threshold: dict[str, int | None] = field(default_factory=lambda: {"image": None, "video": None})
    # lần cuối hit limit per kind: {"t": unix, "count_at_hit": N}
    last_limit_hit: dict[str, dict] = field(default_factory=dict)
    # window override (giây) — mặc định 24h
    window_seconds: int = WINDOW_SECONDS

    def to_dict(self) -> dict:
        return {
            "history": self.history,
            "threshold": self.threshold,
            "last_limit_hit": self.last_limit_hit,
            "window_seconds": self.window_seconds,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QuotaState":
        if not isinstance(d, dict):
            return cls()
        state = cls(
            history=d.get("history", {"image": [], "video": []}),
            threshold=d.get("threshold", {"image": None, "video": None}),
            last_limit_hit=d.get("last_limit_hit", {}),
            window_seconds=d.get("window_seconds", WINDOW_SECONDS),
        )
        # sanitize: file có thể bị sửa tay / ghi nửa chừng — sai type thì vứt
        # field đó dùng default, tuyệt đối không để exception lan lên API.
        if not isinstance(state.history, dict):
            state.history = {"image": [], "video": []}
        for k, v in list(state.history.items()):
            state.history[k] = (
                [t for t in v if isinstance(t, (int, float)) and not isinstance(t, bool)]
                if isinstance(v, list) else []
            )
        if not isinstance(state.threshold, dict):
            state.threshold = {"image": None, "video": None}
        for k, v in list(state.threshold.items()):
            state.threshold[k] = (
                v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None
            )
        if not isinstance(state.last_limit_hit, dict):
            state.last_limit_hit = {}
        state.last_limit_hit = {
            k: v for k, v in state.last_limit_hit.items() if isinstance(v, dict)
        }
        if (not isinstance(state.window_seconds, (int, float))
                or isinstance(state.window_seconds, bool) or state.window_seconds <= 0):
            state.window_seconds = WINDOW_SECONDS
        return state


# --------------------------------------------------------------------------- #
# Account-scoped state (--account) — tách quota theo nhãn, KHÔNG tự đổi account
# --------------------------------------------------------------------------- #
def sanitize_account(account: str | None) -> str | None:
    """Chuẩn hoá label account dùng cho tên file state + truyền qua API.

    None/'' → None (legacy: quota.json chung). Còn lại: trim, giữ chữ-số-_-.,
    ký tự khác → '_' (chống path traversal / ký tự lạ trên tên file), cap 32.
    """
    if account is None:
        return None
    s = str(account).strip()
    if not s:
        return None
    slug = "".join(ch if ch.isalnum() or ch in "_-." else "_" for ch in s)
    return slug[:32]


def state_path(account: str | None, base: Path = DEFAULT_STATE_PATH) -> Path:
    """File state cho account: None → quota.json; có label → quota-<slug>.json."""
    acct = sanitize_account(account)
    if acct is None:
        return base
    return base.with_name(f"quota-{acct}.json")


def load_account(account: str | None, path: Path = DEFAULT_STATE_PATH) -> QuotaState:
    """Load state của 1 account (None → legacy quota.json)."""
    return load(state_path(account, path))


def save_account(state: QuotaState, account: str | None,
                 path: Path = DEFAULT_STATE_PATH) -> None:
    save(state, state_path(account, path))


# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #
def load(path: Path = DEFAULT_STATE_PATH) -> QuotaState:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return QuotaState.from_dict(data)
    except (OSError, ValueError, TypeError, KeyError):
        # FileNotFoundError/PermissionError/IsADirectoryError ⊂ OSError;
        # JSONDecodeError ⊂ ValueError; TypeError/KeyError cho structure sai.
        return QuotaState()


def save(state: QuotaState, path: Path = DEFAULT_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------------- #
def record_success(state: QuotaState, kind: str, now: float | None = None) -> None:
    """Ghi 1 gen thành công vào history."""
    now = now if now is not None else time.time()
    state.history.setdefault(kind, []).append(now)
    _prune(state, kind, now)


def record_limit_hit(state: QuotaState, kind: str, now: float | None = None) -> int:
    """Ghi lần hit limit. Auto-learn threshold = count hiện tại (sau khi đã count).
    Trả về threshold đã học (hoặc giữ nguyên nếu đã học).

    Phân loại limit-hit để CLI/agent biết phải chờ kiểu daily (24h) hay burst
    (rate-limit thoáng, có thể retry sau vài phút):
      - count > 0  → "daily" (đã có history trong window → chắc chắn daily cap)
      - count == 0 → "burst" (chưa có history → nhiều khả năng rate-limit thoáng,
                                không học threshold)
    Lưu vào last_limit_hit[kind]["type"] để snapshot() và CLI đọc lại.
    """
    now = now if now is not None else time.time()
    count = count_recent(state, kind, now)
    # threshold = count (số gen thành công trước khi bị chặn). Nếu bằng 0 thì không học
    # (có thể là rate-limit thời gian ngắn, không phải daily quota).
    if count > 0:
        prev = state.threshold.get(kind)
        # ưu tiên giá trị thấp hơn (conservative) hoặc lần đầu
        if prev is None or count < prev:
            state.threshold[kind] = count
    limit_type = "daily" if count > 0 else "burst"
    state.last_limit_hit[kind] = {"t": now, "count_at_hit": count, "type": limit_type}
    return state.threshold.get(kind) or count


def _prune(state: QuotaState, kind: str, now: float) -> None:
    """Bỏ các entry cũ hơn window (rolling)."""
    cutoff = now - state.window_seconds
    state.history[kind] = [t for t in state.history.get(kind, []) if t >= cutoff]


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def count_recent(state: QuotaState, kind: str, now: float | None = None) -> int:
    """Số gen thành công trong window gần nhất."""
    now = now if now is not None else time.time()
    _prune(state, kind, now)
    return len(state.history.get(kind, []))


def remaining(state: QuotaState, kind: str, now: float | None = None) -> int | None:
    """Số gen còn lại trước khi tới threshold. None nếu chưa học threshold."""
    now = now if now is not None else time.time()
    thr = state.threshold.get(kind)
    if thr is None:
        return None
    return max(0, thr - count_recent(state, kind, now))


def reset_eta_seconds(state: QuotaState, kind: str, now: float | None = None) -> float | None:
    """Số giây tới khi gen cũ nhất trong window bị drop (roll ra khỏi window).
    = khi quota sẽ giảm. None nếu chưa có history."""
    now = now if now is not None else time.time()
    hist = state.history.get(kind, [])
    if not hist:
        return None
    oldest = min(hist)
    return (oldest + state.window_seconds) - now


def oldest_unlock_at(state: QuotaState, kind: str, now: float | None = None) -> float | None:
    """Unix timestamp khi 1 slot quota được unlock (= oldest entry roll ra window).
    None nếu chưa có history. Hữu ích cho agent schedule job chính xác tới thời điểm đó."""
    now = now if now is not None else time.time()
    hist = state.history.get(kind, [])
    if not hist:
        return None
    return min(hist) + state.window_seconds


def classify_limit_hit(state: QuotaState, kind: str) -> str:
    """Phân loại lần hit limit cuối: "daily" / "burst" / "unknown".

    Dựa trên last_limit_hit[kind]["type"] mà record_limit_hit() đã lưu. Trả "unknown"
    nếu kind chưa từng hit limit. CLI dùng để chọn hint phù hợp:
      - daily → "chờ rolling window (có thể vài giờ)"
      - burst → "thử lại sau 5-10 phút"
      - unknown → không có history để phán đoán
    """
    hit = state.last_limit_hit.get(kind)
    if not isinstance(hit, dict):
        return "unknown"
    return hit.get("type") or "unknown"


def is_exhausted_message(text: str) -> bool:
    """Check xem text bot message có phải báo quota hết không."""
    low = text.lower()
    return any(p in low for p in QUOTA_EXHAUSTED_PATTERNS)


def plan_retry(info: dict | None, kind: str | None = None, now: float | None = None, *,
               burst_retry_seconds: float = 300.0,
               unknown_retry_seconds: float = 120.0,
               resume_buffer_seconds: float = 15.0) -> tuple[float | None, str]:
    """Tính thời gian chờ (giây) trước khi re-enqueue 1 job bị quota từ chối.

    info = snapshot quota của kind (từ detail.quota của 429 hoặc job["quota"]).
    Nhận cả 2 shape: sub-dict ({"remaining": …}) hoặc wrapped ({"image": {…}})
    khi truyền kèm `kind` (driver trả wrapped, server 429 trả unwrapped).

    Trả (delay, reason):
      - daily cap → chờ tới khi slot cũ nhất roll ra window + buffer an toàn
        (ưu tiên oldest_unlock_at; fallback reset_in_seconds thành timestamp).
      - burst     → rate-limit thoáng, chờ burst_retry_seconds rồi thử lại.
      - unknown   → không rõ, chờ unknown_retry_seconds (fail-safe).
      - (None, _) → thiếu dữ kiện — caller tự chọn fallback.

    Pure + clock injectable (now) — deterministic trong test.
    """
    now = now if now is not None else time.time()
    if kind and isinstance(info, dict) and isinstance(info.get(kind), dict):
        info = info[kind]
    if not isinstance(info, dict):
        return None, "no quota info"
    ltype = info.get("last_limit_type")
    if ltype == "daily":
        unlock = info.get("oldest_unlock_at")
        if not isinstance(unlock, (int, float)) or isinstance(unlock, bool):
            eta = info.get("reset_in_seconds")
            if isinstance(eta, (int, float)) and not isinstance(eta, bool):
                unlock = now + eta
        if isinstance(unlock, (int, float)) and not isinstance(unlock, bool):
            return max(unlock + resume_buffer_seconds - now, 0.0), "daily cap"
        return None, "daily cap, không rõ thời điểm unlock"
    if ltype == "burst":
        return burst_retry_seconds, "rate-limit burst"
    return unknown_retry_seconds, "limit type unknown"


def snapshot(state: QuotaState, kind: str | None = None, now: float | None = None) -> dict:
    """Trả dict summary để hiển thị (cici quota / API).

    Trường bổ sung (cho agent scheduler + CLI hint):
      - oldest_unlock_at     : unix ts khi 1 slot unlock (None nếu không có history)
      - last_limit_type      : "daily" / "burst" / None (phân loại lần hit cuối)
      - suggested_retry_after: seconds — tiện cho agent không cần tự tính
    """
    now = now if now is not None else time.time()
    kinds = [kind] if kind else ["image", "video"]
    out = {}
    for k in kinds:
        cnt = count_recent(state, k, now)
        thr = state.threshold.get(k)
        rmn = remaining(state, k, now)
        eta = reset_eta_seconds(state, k, now)
        unlock = oldest_unlock_at(state, k, now)
        hit = state.last_limit_hit.get(k)
        out[k] = {
            "used_in_window": cnt,
            "threshold": thr,
            "remaining": rmn,
            "reset_in_seconds": round(eta, 0) if eta is not None else None,
            "oldest_unlock_at": round(unlock, 0) if unlock is not None else None,
            "last_limit_hit_at": hit.get("t") if hit else None,
            "last_limit_type": hit.get("type") if hit else None,
            "suggested_retry_after": max(round(eta, 0), 0) if eta is not None else None,
            "window_hours": round(state.window_seconds / 3600, 1),
        }
    return out