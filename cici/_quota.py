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
        return cls(
            history=d.get("history", {"image": [], "video": []}),
            threshold=d.get("threshold", {"image": None, "video": None}),
            last_limit_hit=d.get("last_limit_hit", {}),
            window_seconds=d.get("window_seconds", WINDOW_SECONDS),
        )


# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #
def load(path: Path = DEFAULT_STATE_PATH) -> QuotaState:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return QuotaState.from_dict(data)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
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
    Trả về threshold đã học (hoặc giữ nguyên nếu đã học)."""
    now = now if now is not None else time.time()
    count = count_recent(state, kind, now)
    # threshold = count (số gen thành công trước khi bị chặn). Nếu bằng 0 thì không học
    # (có thể là rate-limit thời gian ngắn, không phải daily quota).
    if count > 0:
        prev = state.threshold.get(kind)
        # ưu tiên giá trị thấp hơn (conservative) hoặc lần đầu
        if prev is None or count < prev:
            state.threshold[kind] = count
    state.last_limit_hit[kind] = {"t": now, "count_at_hit": count}
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


def is_exhausted_message(text: str) -> bool:
    """Check xem text bot message có phải báo quota hết không."""
    low = text.lower()
    return any(p in low for p in QUOTA_EXHAUSTED_PATTERNS)


def snapshot(state: QuotaState, kind: str | None = None, now: float | None = None) -> dict:
    """Trả dict summary để hiển thị (cici quota / API)."""
    now = now if now is not None else time.time()
    kinds = [kind] if kind else ["image", "video"]
    out = {}
    for k in kinds:
        cnt = count_recent(state, k, now)
        thr = state.threshold.get(k)
        rmn = remaining(state, k, now)
        eta = reset_eta_seconds(state, k, now)
        hit = state.last_limit_hit.get(k)
        out[k] = {
            "used_in_window": cnt,
            "threshold": thr,
            "remaining": rmn,
            "reset_in_seconds": round(eta, 0) if eta is not None else None,
            "last_limit_hit_at": hit.get("t") if hit else None,
            "window_hours": round(state.window_seconds / 3600, 1),
        }
    return out