"""Deterministic tests cho quota mở rộng + adaptive polling (không cần server, không tốn quota).

Test các hàm mới trong cici/_quota.py (classify_limit_hit, oldest_unlock_at, snapshot
extension, plan_retry) + cici/_client.wait_status với poll backoff + retry loop
--wait-for-quota trong cici/cli.py (mock enqueue/poll, không cần server). Style
hand-rolled script — khớp với test_wait_status.py / test_result_detection.py.

    python tests/test_quota.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cici import _client, _quota  # noqa: E402


def main() -> int:
    passed = 0

    # ------------------------------------------------------------------ #
    # classify_limit_hit
    # ------------------------------------------------------------------ #
    # 1. fresh state (chưa hit limit) → unknown
    s = _quota.QuotaState()
    cls = _quota.classify_limit_hit(s, "image")
    if cls == "unknown":
        passed += 1
        print("PASS 1: classify_limit_hit fresh → 'unknown'")
    else:
        print(f"FAIL 1: classify fresh expected 'unknown' got {cls!r}")

    # 2. record_limit_hit với count=0 (no history) → burst
    s2 = _quota.QuotaState()
    _quota.record_limit_hit(s2, "image", now=1000.0)
    cls2 = _quota.classify_limit_hit(s2, "image")
    if cls2 == "burst":
        passed += 1
        print("PASS 2: classify_limit_hit count=0 → 'burst'")
    else:
        print(f"FAIL 2: classify burst expected 'burst' got {cls2!r}")

    # 3. record_limit_hit với count>0 (có history) → daily
    s3 = _quota.QuotaState()
    _quota.record_success(s3, "image", now=900.0)
    _quota.record_limit_hit(s3, "image", now=1000.0)
    cls3 = _quota.classify_limit_hit(s3, "image")
    if cls3 == "daily":
        passed += 1
        print("PASS 3: classify_limit_hit count>0 → 'daily'")
    else:
        print(f"FAIL 3: classify daily expected 'daily' got {cls3!r}")

    # ------------------------------------------------------------------ #
    # oldest_unlock_at
    # ------------------------------------------------------------------ #
    # 4. fresh → None
    s4 = _quota.QuotaState()
    unlock = _quota.oldest_unlock_at(s4, "image", now=5000.0)
    if unlock is None:
        passed += 1
        print("PASS 4: oldest_unlock_at fresh → None")
    else:
        print(f"FAIL 4: oldest fresh expected None got {unlock}")

    # 5. sau 1 gen ở t=1000, window 24h → oldest unlock = 1000 + 86400
    s5 = _quota.QuotaState()
    _quota.record_success(s5, "image", now=1000.0)
    unlock5 = _quota.oldest_unlock_at(s5, "image", now=2000.0)
    if unlock5 == 1000.0 + 86400.0:
        passed += 1
        print("PASS 5: oldest_unlock_at = oldest + window_seconds")
    else:
        print(f"FAIL 5: oldest expected {1000.0 + 86400.0} got {unlock5}")

    # ------------------------------------------------------------------ #
    # snapshot new fields
    # ------------------------------------------------------------------ #
    # 6. snapshot sau record_limit_hit có last_limit_type
    s6 = _quota.QuotaState()
    _quota.record_success(s6, "image", now=900.0)
    _quota.record_limit_hit(s6, "image", now=1000.0)
    snap = _quota.snapshot(s6, "image", now=1000.0)["image"]
    if snap.get("last_limit_type") == "daily":
        passed += 1
        print("PASS 6: snapshot.last_limit_type == 'daily' after limit hit")
    else:
        print(f"FAIL 6: snapshot last_limit_type expected 'daily' got {snap.get('last_limit_type')!r}")

    # 7. snapshot có oldest_unlock_at
    if isinstance(snap.get("oldest_unlock_at"), (int, float)) and snap["oldest_unlock_at"] > 1000.0:
        passed += 1
        print("PASS 7: snapshot.oldest_unlock_at là unix timestamp trong tương lai")
    else:
        print(f"FAIL 7: oldest_unlock_at không hợp lệ: {snap.get('oldest_unlock_at')!r}")

    # 8. snapshot có suggested_retry_after = reset_in_seconds
    if snap.get("suggested_retry_after") == snap.get("reset_in_seconds"):
        passed += 1
        print("PASS 8: snapshot.suggested_retry_after == reset_in_seconds")
    else:
        print(f"FAIL 8: suggested_retry_after {snap.get('suggested_retry_after')} != "
              f"reset_in_seconds {snap.get('reset_in_seconds')}")

    # 9. snapshot throttling key cũ vẫn còn (backward compat)
    required_old = ("used_in_window", "threshold", "remaining", "reset_in_seconds",
                    "last_limit_hit_at", "window_hours")
    missing = [k for k in required_old if k not in snap]
    if not missing:
        passed += 1
        print("PASS 9: snapshot vẫn có tất cả key cũ (backward compat)")
    else:
        print(f"FAIL 9: snapshot thiếu key cũ: {missing}")

    # ------------------------------------------------------------------ #
    # Adaptive polling
    # ------------------------------------------------------------------ #
    # 10. PENDING liên tục: tổng sleep phải tăng dần (backoff).
    # Trick: poll_interval nhỏ, status luôn PENDING. Đo tổng elapsed > sum(sleep).
    from cici import _client as _cli

    polls = {"n": 0}

    def pending_fn(jid):
        polls["n"] += 1
        return {"status": "PENDING", "queue_ahead": 0}

    t0 = time.time()
    try:
        _cli.wait_status("adaptive-pending", timeout=0.4, poll_interval=0.02,
                         poll_max_interval=0.1, status_fn=pending_fn)
    except TimeoutError:
        pass
    elapsed = time.time() - t0
    # poll ở status PENDING dùng backoff: 0.02, 0.026, 0.0338, 0.044, 0.0572, 0.0744, 0.0967, 0.1(cap), ...
    # Với timeout=0.4 ta có ~10-15 polls; mỗi sleep sau poll đầu tăng → elapsed > polls * poll_interval đơn thuần.
    # Mức tối thiểu: nếu KHÔNG có backoff, elapsed ≈ polls * 0.02 (mỗi poll sleep 0.02).
    # Với backoff, elapsed sẽ lớn hơn đáng kể (cuối cùng đạt cap 0.1).
    if polls["n"] >= 5 and elapsed > polls["n"] * 0.03:
        passed += 1
        print(f"PASS 10: adaptive backoff hoạt động (polls={polls['n']}, elapsed={elapsed:.3f}s)")
    else:
        print(f"FAIL 10: backoff không hoạt động (polls={polls['n']}, elapsed={elapsed:.3f}s)")

    # 11. PROCESSING giữ poll_interval cố định (không backoff)
    polls2 = {"n": 0}

    def processing_fn(jid):
        polls2["n"] += 1
        return {"status": "PROCESSING"}

    t1 = time.time()
    try:
        _cli.wait_status("adaptive-processing", timeout=0.3, poll_interval=0.02,
                         poll_max_interval=0.1, status_fn=processing_fn)
    except TimeoutError:
        pass
    elapsed2 = time.time() - t1
    # PROCESSING: sleep luôn 0.02 → elapsed ≈ polls2 * 0.02 + (timeout overhead)
    if polls2["n"] >= 5 and elapsed2 < polls2["n"] * 0.05:
        passed += 1
        print(f"PASS 11: PROCESSING poll giữ interval cố định "
              f"(polls={polls2['n']}, elapsed={elapsed2:.3f}s)")
    else:
        print(f"FAIL 11: PROCESSING polling không ổn định (polls={polls2['n']}, elapsed={elapsed2:.3f}s)")

    # 12. status đổi PENDING → PROCESSING → COMPLETED reset poll counter
    polls3 = {"n": 0}
    states3 = ["PENDING"] * 8 + ["PROCESSING"] * 3 + ["COMPLETED"]

    def changing_fn(jid):
        polls3["n"] += 1
        i = min(polls3["n"] - 1, len(states3) - 1)
        return {"status": states3[i], "queue_ahead": 0}

    t2 = time.time()
    res = _cli.wait_status("adaptive-change", timeout=2.0, poll_interval=0.02,
                           poll_max_interval=0.1, status_fn=changing_fn)
    elapsed3 = time.time() - t2
    if res["status"] == "COMPLETED":
        passed += 1
        print(f"PASS 12: status change → counter reset (polls={polls3['n']}, elapsed={elapsed3:.3f}s)")
    else:
        print(f"FAIL 12: kết quả cuối không phải COMPLETED: {res.get('status')}")

    # ------------------------------------------------------------------ #
    # JSON file persistence (cici/_persist.py) — basic round-trip
    # ------------------------------------------------------------------ #
    # 13. save → load round-trip giữ entry data (trừ ephemeral keys)
    with tempfile.TemporaryDirectory(prefix="cici_quota_test_") as td:
        from cici import _persist
        p = Path(td) / "jobs.json"
        sample = {
            "j1": {"status": "COMPLETED", "kind": "image", "queue_ahead": 0, "queue_size": 2,
                   "result_urls": ["http://x"]},
            "j2": {"status": "PENDING", "kind": "video"},
        }
        _persist.save_jobs(sample, p)
        loaded = _persist.load_jobs(p)
        if loaded.get("j1", {}).get("status") == "COMPLETED" \
                and "queue_ahead" not in loaded.get("j1", {}) \
                and loaded.get("j2", {}).get("kind") == "video":
            passed += 1
            print("PASS 13: save_jobs / load_jobs round-trip OK (ephemeral keys stripped)")
        else:
            print(f"FAIL 13: round-trip sai: {loaded}")

        # 14. corrupt file → empty dict (fail-open)
        bad = Path(td) / "bad.json"
        bad.write_text('{"j1": {"status": "OK"', encoding="utf-8")  # JSON đứt
        empty = _persist.load_jobs(bad)
        if empty == {}:
            passed += 1
            print("PASS 14: load_jobs corrupt → empty dict (fail-open)")
        else:
            print(f"FAIL 14: corrupt không trả {{}}: {empty}")

        # 15. reconcile_on_boot: PENDING/PROCESSING → FAILED
        jobs_inflight = {
            "a": {"status": "PENDING"},
            "b": {"status": "PROCESSING"},
            "c": {"status": "COMPLETED"},
        }
        n = _persist.reconcile_on_boot(jobs_inflight, now=5000.0)
        if n == 2 and jobs_inflight["a"]["status"] == "FAILED" \
                and jobs_inflight["b"]["status"] == "FAILED" \
                and jobs_inflight["c"]["status"] == "COMPLETED":
            passed += 1
            print("PASS 15: reconcile_on_boot: PENDING/PROCESSING → FAILED, COMPLETED giữ nguyên")
        else:
            print(f"FAIL 15: reconcile sai: n={n} jobs={jobs_inflight}")

    # 16. load_jobs trên file không tồn tại → empty dict
    try:
        from cici import _persist
        nope = Path(tempfile.gettempdir()) / "cici_quota_does_not_exist_xyz.json"
        if nope.exists():
            nope.unlink()
        empty2 = _persist.load_jobs(nope)
        if empty2 == {}:
            passed += 1
            print("PASS 16: load_jobs file missing → empty dict")
        else:
            print(f"FAIL 16: missing file không trả {{}}: {empty2}")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL 16: load_jobs missing raise {type(e).__name__}: {e}")

    # ------------------------------------------------------------------ #
    # plan_retry (--wait-for-quota scheduling) — pure, clock injectable
    # ------------------------------------------------------------------ #
    # 17. daily + oldest_unlock_at tương lai → delay = unlock + buffer − now
    d17, r17 = _quota.plan_retry({"last_limit_type": "daily", "oldest_unlock_at": 5000.0},
                                 now=1000.0, resume_buffer_seconds=15.0)
    if d17 == 5000.0 + 15.0 - 1000.0 and r17 == "daily cap":
        passed += 1
        print("PASS 17: plan_retry daily → unlock + buffer − now")
    else:
        print(f"FAIL 17: plan_retry daily cho {d17!r}, {r17!r}")

    # 18. burst → chờ cố định burst_retry_seconds
    d18, r18 = _quota.plan_retry({"last_limit_type": "burst"}, now=1000.0,
                                 burst_retry_seconds=300.0)
    if d18 == 300.0 and r18 == "rate-limit burst":
        passed += 1
        print("PASS 18: plan_retry burst → fixed delay")
    else:
        print(f"FAIL 18: plan_retry burst cho {d18!r}, {r18!r}")

    # 19. unknown (info rỗng) → unknown_retry_seconds
    d19, _r19 = _quota.plan_retry({}, now=1000.0, unknown_retry_seconds=120.0)
    if d19 == 120.0:
        passed += 1
        print("PASS 19: plan_retry unknown → fallback delay")
    else:
        print(f"FAIL 19: plan_retry unknown cho {d19!r}")

    # 20. shape wrapped {"image": {...}} (driver trả) + kind → unwrap đúng
    d20, r20 = _quota.plan_retry(
        {"image": {"last_limit_type": "daily", "oldest_unlock_at": 5000.0}},
        kind="image", now=1000.0, resume_buffer_seconds=15.0)
    if d20 == 5000.0 + 15.0 - 1000.0 and r20 == "daily cap":
        passed += 1
        print("PASS 20: plan_retry unwrap wrapped snapshot theo kind")
    else:
        print(f"FAIL 20: plan_retry wrapped cho {d20!r}, {r20!r}")

    # 21. info None → (None, …) — caller dùng fallback
    d21, _r21 = _quota.plan_retry(None, now=1000.0)
    if d21 is None:
        passed += 1
        print("PASS 21: plan_retry None → None (caller fallback)")
    else:
        print(f"FAIL 21: plan_retry None cho {d21!r}")

    # 22. daily thiếu cả oldest_unlock_at lẫn reset_in_seconds → None
    d22, _r22 = _quota.plan_retry({"last_limit_type": "daily"}, now=1000.0)
    if d22 is None:
        passed += 1
        print("PASS 22: plan_retry daily thiếu ETA → None")
    else:
        print(f"FAIL 22: plan_retry daily-no-eta cho {d22!r}")

    # 23. daily chỉ có reset_in_seconds (ETA) → chuyển thành timestamp rồi tính
    d23, r23 = _quota.plan_retry({"last_limit_type": "daily", "reset_in_seconds": 3600.0},
                                 now=1000.0, resume_buffer_seconds=0.0)
    if d23 == 3600.0 and r23 == "daily cap":
        passed += 1
        print("PASS 23: plan_retry daily dùng reset_in_seconds fallback")
    else:
        print(f"FAIL 23: plan_retry daily-eta cho {d23!r}, {r23!r}")

    # ------------------------------------------------------------------ #
    # CLI --wait-for-quota retry loop (mock enqueue/poll — không cần server)
    # ------------------------------------------------------------------ #
    import cici.cli as _cli_mod  # noqa: E402

    calls = {"generate": 0, "sleep": 0}

    def fake_generate(*_a, **_kw):
        calls["generate"] += 1
        if calls["generate"] < 2:
            raise _client.QuotaExhausted({
                "message": "quota cạn (mock)",
                "quota": {"last_limit_type": "daily",
                          "oldest_unlock_at": time.time() + 5.0},
            })
        return {"job_id": "mock-job", "timeout_s": 1}

    def fake_wait(*_a, **_kw):
        return {"status": "COMPLETED", "result_urls": []}

    def fake_sleep(delay, _reason, _kind, _attempt):
        calls["sleep"] += 1

    _orig = (_cli_mod._preflight, _client.generate, _client.wait_status, _cli_mod._quota_sleep)
    _cli_mod._preflight = lambda base, auto_launch=True, provider="cici": True
    _client.generate = fake_generate
    _client.wait_status = fake_wait
    _cli_mod._quota_sleep = fake_sleep
    try:
        # 24. 429 lần đầu → chờ (sleep) → re-enqueue lần 2 COMPLETED (exit 0)
        code24 = _cli_mod._run_generation("mock", "image", False, "http://mock",
                                          quota_wait=True)
        if code24 == _client.EXIT_OK and calls["generate"] == 2 and calls["sleep"] == 1:
            passed += 1
            print("PASS 24: CLI --wait-for-quota retry 1 lần rồi COMPLETED (exit 0)")
        else:
            print(f"FAIL 24: exit={code24} generate={calls['generate']} sleep={calls['sleep']}")
    finally:
        _cli_mod._preflight, _client.generate, _client.wait_status, _cli_mod._quota_sleep = _orig

    # 25. 429 liên tục → hết quota.max_attempts (3) → exit 4, chỉ sleep 2 lần
    calls2 = {"generate": 0, "sleep": 0}

    def fake_generate_always(*_a, **_kw):
        calls2["generate"] += 1
        raise _client.QuotaExhausted({
            "message": "quota cạn (mock)",
            "quota": {"last_limit_type": "daily",
                      "oldest_unlock_at": time.time() + 5.0},
        })

    def fake_sleep2(delay, _reason, _kind, _attempt):
        calls2["sleep"] += 1

    _orig2 = (_cli_mod._preflight, _client.generate, _client.wait_status, _cli_mod._quota_sleep)
    _cli_mod._preflight = lambda base, auto_launch=True, provider="cici": True
    _client.generate = fake_generate_always
    _client.wait_status = fake_wait
    _cli_mod._quota_sleep = fake_sleep2
    try:
        code25 = _cli_mod._run_generation("mock", "image", False, "http://mock",
                                          quota_wait=True)
        if code25 == _client.EXIT_QUOTA and calls2["generate"] == 3 and calls2["sleep"] == 2:
            passed += 1
            print("PASS 25: CLI --wait-for-quota hết attempts → exit 4 (bounded)")
        else:
            print(f"FAIL 25: exit={code25} generate={calls2['generate']} sleep={calls2['sleep']}")
    finally:
        _cli_mod._preflight, _client.generate, _client.wait_status, _cli_mod._quota_sleep = _orig2

    print(f"\n{passed}/25 tests passed")
    return 0 if passed == 25 else 1


if __name__ == "__main__":
    sys.exit(main())
