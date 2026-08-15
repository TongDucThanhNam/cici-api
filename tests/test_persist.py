"""Deterministic tests cho job persistence (cici/_persist.py — không cần server).

Cover load/save round-trip, fail-open khi file corrupt, strip ephemeral keys,
reconcile_on_boot (in-flight → FAILED), merge_into_store, và retention pruning
(job terminal cũ bị bỏ, in-flight luôn giữ). Style hand-rolled script — khớp
với test_wait_status.py / test_quota.py.

    python tests/test_persist.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cici import _persist  # noqa: E402


def main() -> int:
    passed = 0

    with tempfile.TemporaryDirectory(prefix="cici_persist_test_") as td:
        path = Path(td) / "jobs.json"

        # ------------------------------------------------------------------ #
        # load_jobs: file thiếu / corrupt / sai structure → fail-open
        # ------------------------------------------------------------------ #
        # 1. file chưa tồn tại → dict rỗng
        if _persist.load_jobs(path) == {}:
            passed += 1
            print("PASS 1: load_jobs file thiếu → {} (fail-open)")
        else:
            print("FAIL 1: load_jobs file thiếu phải trả {}")

        # 2. JSON đứt giữa chừng → dict rỗng
        path.write_text('{"job1": {"status": "COMP', encoding="utf-8")
        if _persist.load_jobs(path) == {}:
            passed += 1
            print("PASS 2: load_jobs JSON corrupt → {} (fail-open)")
        else:
            print("FAIL 2: load_jobs JSON corrupt phải trả {}")

        # 3. top-level không phải dict → dict rỗng
        path.write_text('["not", "a", "dict"]', encoding="utf-8")
        if _persist.load_jobs(path) == {}:
            passed += 1
            print("PASS 3: load_jobs top-level list → {} (fail-open)")
        else:
            print("FAIL 3: load_jobs top-level list phải trả {}")

        # ------------------------------------------------------------------ #
        # save/load round-trip + strip ephemeral keys + defaults
        # ------------------------------------------------------------------ #
        now = time.time()
        jobs = {
            "j1": {"status": "COMPLETED", "kind": "image", "prompt": "meo",
                   "result_urls": ["https://x/1.jpeg"], "seq": 1,
                   "created_at": now - 10, "finished_at": now - 5,
                   "queue_ahead": 3, "queue_size": 7},
            "j2": {"status": "PROCESSING", "kind": "video", "seq": 2,
                   "created_at": now - 2},
        }
        _persist.save_jobs(jobs, path)  # type: ignore[arg-type]
        loaded = _persist.load_jobs(path)

        # 4. round-trip giữ đủ entry
        if set(loaded) == {"j1", "j2"}:
            passed += 1
            print("PASS 4: save → load round-trip giữ đủ entry")
        else:
            print(f"FAIL 4: round-trip mất entry: {set(loaded)}")

        # 5. ephemeral keys không xuống file
        if "queue_ahead" not in loaded["j1"] and "queue_size" not in loaded["j1"]:
            passed += 1
            print("PASS 5: queue_ahead/queue_size bị strip khi save")
        else:
            print("FAIL 5: ephemeral keys lọt xuống file")

        # 6. entry thiếu field cốt lõi → default khi load
        path.write_text(json.dumps({"j3": {"prompt": "bare"}}), encoding="utf-8")
        l3 = _persist.load_jobs(path)
        if l3["j3"]["status"] == "UNKNOWN" and l3["j3"]["created_at"] == 0.0:
            passed += 1
            print("PASS 6: entry thiếu field → default (status/kind/created_at)")
        else:
            print(f"FAIL 6: default không áp dụng: {l3}")

        # ------------------------------------------------------------------ #
        # reconcile_on_boot: in-flight → FAILED, terminal giữ nguyên
        # ------------------------------------------------------------------ #
        # 7.
        boot = {
            "p": {"status": "PENDING", "created_at": now - 1},
            "r": {"status": "PROCESSING", "created_at": now - 1},
            "c": {"status": "COMPLETED", "created_at": now - 1},
        }
        n = _persist.reconcile_on_boot(boot, now=now)
        if (n == 2 and boot["p"]["status"] == "FAILED" and boot["r"]["status"] == "FAILED"
                and boot["c"]["status"] == "COMPLETED"
                and "server restarted" in boot["p"]["error"]):
            passed += 1
            print("PASS 7: reconcile_on_boot PENDING/PROCESSING → FAILED (2 job), COMPLETED giữ")
        else:
            print(f"FAIL 7: reconcile sai: n={n}, boot={boot}")

        # ------------------------------------------------------------------ #
        # merge_into_store: loaded thắng, đếm đúng
        # ------------------------------------------------------------------ #
        # 8.
        store: dict = {"old": {"status": "COMPLETED"}}
        cnt = _persist.merge_into_store(store, {"new": {"status": "FAILED"},
                                                "old": {"status": "COMPLETED", "seq": 9}})
        if cnt == 2 and store["old"]["seq"] == 9 and store["new"]["status"] == "FAILED":
            passed += 1
            print("PASS 8: merge_into_store thêm/cập nhật entry (loaded thắng)")
        else:
            print(f"FAIL 8: merge sai: cnt={cnt}, store={store}")

        # ------------------------------------------------------------------ #
        # prune_jobs: retention
        # ------------------------------------------------------------------ #
        old_ts = time.time() - 8 * 24 * 3600  # 8 ngày trước (> retention 7 ngày)
        fresh_ts = time.time() - 3600
        src = {
            "old_done": {"status": "COMPLETED", "created_at": old_ts, "finished_at": old_ts},
            "old_failed": {"status": "FAILED", "created_at": old_ts, "finished_at": old_ts},
            "new_done": {"status": "COMPLETED", "created_at": fresh_ts, "finished_at": fresh_ts},
            "old_pending": {"status": "PENDING", "created_at": old_ts},
            "old_processing": {"status": "PROCESSING", "created_at": old_ts},
            "old_weird": {"status": "SOMETHING_ELSE", "created_at": old_ts},
            "old_no_ts": {"status": "COMPLETED"},
        }

        # 9. terminal cũ bị bỏ; mới / in-flight / status lạ / thiếu ts giữ
        pruned = _persist.prune_jobs(src)
        kept = set(pruned)
        if kept == {"new_done", "old_pending", "old_processing", "old_weird", "old_no_ts"}:
            passed += 1
            print("PASS 9: prune bỏ terminal cũ, giữ mới/in-flight/lạ/thiếu ts")
        else:
            print(f"FAIL 9: prune giữ sai: {kept}")

        # 10. prune KHÔNG mutate dict đầu vào
        if set(src) == {"old_done", "old_failed", "new_done", "old_pending",
                        "old_processing", "old_weird", "old_no_ts"}:
            passed += 1
            print("PASS 10: prune_jobs không mutate dict đầu vào")
        else:
            print("FAIL 10: prune_jobs mutate dict đầu vào")

        # 11. keep_seconds=None → tắt prune
        if set(_persist.prune_jobs(src, keep_seconds=None)) == set(src):
            passed += 1
            print("PASS 11: keep_seconds=None → không prune gì")
        else:
            print("FAIL 11: keep_seconds=None vẫn prune")

        # 12. finished_at cũ nhưng created_at mới → vẫn bị bỏ (dùng finished_at)
        mixed = {"m": {"status": "COMPLETED", "created_at": fresh_ts, "finished_at": old_ts}}
        if "m" not in _persist.prune_jobs(mixed):
            passed += 1
            print("PASS 12: prune ưu tiên finished_at khi có")
        else:
            print("FAIL 12: prune bỏ qua finished_at")

        # ------------------------------------------------------------------ #
        # save_jobs tích hợp retention
        # ------------------------------------------------------------------ #
        # 13. save mặc định prune terminal cũ khỏi file
        p2 = Path(td) / "ret.json"
        _persist.save_jobs(src, p2)  # type: ignore[arg-type]
        on_disk = set(_persist.load_jobs(p2))
        if on_disk == {"new_done", "old_pending", "old_processing", "old_weird", "old_no_ts"}:
            passed += 1
            print("PASS 13: save_jobs mặc định áp retention khi ghi file")
        else:
            print(f"FAIL 13: file sau save sai: {on_disk}")

        # 14. save_jobs(keep_seconds=None) giữ nguyên tất cả
        p3 = Path(td) / "noret.json"
        _persist.save_jobs(src, p3, keep_seconds=None)  # type: ignore[arg-type]
        if set(_persist.load_jobs(p3)) == set(src):
            passed += 1
            print("PASS 14: save_jobs keep_seconds=None → giữ tất cả")
        else:
            print("FAIL 14: keep_seconds=None vẫn prune khi save")

        # 15. save_jobs không mutate store trong RAM (input dict giữ đủ entry)
        if set(src) == {"old_done", "old_failed", "new_done", "old_pending",
                        "old_processing", "old_weird", "old_no_ts"}:
            passed += 1
            print("PASS 15: save_jobs không mutate store trong RAM")
        else:
            print("FAIL 15: save_jobs mutate store trong RAM")

    print(f"\n{passed}/15 tests passed")
    return 0 if passed == 15 else 1


if __name__ == "__main__":
    sys.exit(main())
