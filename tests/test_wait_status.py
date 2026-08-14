"""Deterministic tests cho queue-aware wait_status + queue_ahead (không cần server).

    python tests/test_wait_status.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cici import _client  # noqa: E402
from cici_driver import JobStore  # noqa: E402
from main import queue_ahead  # noqa: E402


def make_poller(states):
    """Tạo status_fn trả lần lượt các state. State cuối lặp vô hạn."""
    calls = {"n": 0}

    def fn(job_id):
        i = min(calls["n"], len(states) - 1)
        calls["n"] += 1
        s = states[i]
        return {"status": s, "queue_ahead": 2} if s == "PENDING" else {"status": s}

    return fn


def main() -> int:
    passed = 0

    # 1. PENDING -> PROCESSING -> COMPLETED: trả kết quả, không timeout
    fn = make_poller(["PENDING", "PROCESSING", "COMPLETED"])
    res = _client.wait_status("j1", timeout=5.0, poll_interval=0.01, status_fn=fn)
    assert res["status"] == "COMPLETED", res
    passed += 1
    print("PASS 1: normal PENDING -> PROCESSING -> COMPLETED")

    # 2. Kẹt PENDING với queue_ahead=2: TimeoutError trong cap ~timeout*(2+1)
    fn = make_poller(["PENDING"])
    t0 = time.time()
    try:
        _client.wait_status("j2", timeout=0.1, poll_interval=0.01, status_fn=fn)
        raise AssertionError("expected TimeoutError")
    except TimeoutError as e:
        assert "queue" in str(e), e
    elapsed = time.time() - t0
    assert elapsed < 2.0, f"queue cap chạy quá lâu: {elapsed:.2f}s"
    passed += 1
    print(f"PASS 2: PENDING queue wait capped (elapsed {elapsed:.2f}s)")

    # 3. PROCESSING chạy quá timeout: TimeoutError chỉ tính thời gian xử lý
    fn = make_poller(["PENDING", "PROCESSING"])
    t0 = time.time()
    try:
        _client.wait_status("j3", timeout=0.1, poll_interval=0.01, status_fn=fn)
        raise AssertionError("expected TimeoutError")
    except TimeoutError as e:
        assert "PROCESSING" in str(e), e
    elapsed = time.time() - t0
    assert elapsed < 2.0, f"processing budget sai: {elapsed:.2f}s"
    passed += 1
    print(f"PASS 3: PROCESSING budget enforced (elapsed {elapsed:.2f}s)")

    # 4. COMPLETED ngay từ poll đầu: trả về luôn, không sleep
    fn = make_poller(["COMPLETED"])
    t0 = time.time()
    res = _client.wait_status("j4", timeout=5.0, poll_interval=0.01, status_fn=fn)
    assert res["status"] == "COMPLETED" and time.time() - t0 < 1.0
    passed += 1
    print("PASS 4: immediate COMPLETED returns without waiting")

    # 5. on_tick nhận mọi poll kể cả poll đầu
    seen = []
    fn = make_poller(["PENDING", "COMPLETED"])
    _client.wait_status("j5", timeout=5.0, poll_interval=0.01, status_fn=fn,
                        on_tick=lambda s: seen.append(s["status"]))
    assert seen[0] == "PENDING" and "COMPLETED" in seen, seen
    passed += 1
    print("PASS 5: on_tick fires on every poll including the first")

    # 6. queue_ahead tính đúng số job PENDING enqueue trước
    store = JobStore()
    store.set("a", status="PENDING", seq=1)
    store.set("b", status="PENDING", seq=2)
    store.set("c", status="PROCESSING", seq=3)   # đang xử lý — không nằm trong hàng đợi
    store.set("d", status="COMPLETED", seq=4)    # xong — không tính
    assert queue_ahead(store, 1) == 0
    assert queue_ahead(store, 2) == 1
    assert queue_ahead(store, 5) == 2
    passed += 1
    print("PASS 6: queue_ahead counts only PENDING jobs enqueued earlier")

    print(f"\n{passed}/6 tests passed")
    return 0 if passed == 6 else 1


if __name__ == "__main__":
    sys.exit(main())
