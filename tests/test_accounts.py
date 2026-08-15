"""Deterministic tests cho --account (tách quota local theo nhãn account).

Không cần server/mạng (fake httpx cho payload check) — hand-rolled script:

    python tests/test_accounts.py

Phạm vi tính năng: tool KHÔNG tự đổi account, không đụng login/session — label
chỉ để đếm rolling 24h + threshold riêng cho từng account người dùng tự đổi.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cici import _client, _quota  # noqa: E402


class _FakeResp:
    def __init__(self, data=None):
        self._data = data or {}
        self.status_code = 200

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class _FakeClient:
    calls: list = []

    def __init__(self, timeout):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json):
        _FakeClient.calls.append(("POST", url, json))
        return _FakeResp({"job_id": "j1", "status": "PENDING", "timeout_s": 1})

    def get(self, url, params):
        _FakeClient.calls.append(("GET", url, params))
        return _FakeResp({})


def main() -> int:
    passed = 0

    # ------------------------------------------------------------------ #
    # sanitize_account / state_path
    # ------------------------------------------------------------------ #
    # 1. chuẩn hoá label: None/'' → None, trim, ký tự lạ → '_', giữ _-., cap 32
    cases = [
        (None, None), ("", None), ("   ", None),
        ("acc1", "acc1"), ("  acc 1  ", "acc_1"),
        ("../evil", ".._evil"), ("my.account-1", "my.account-1"),
        ("a" * 40, "a" * 32),
    ]
    got = [_quota.sanitize_account(c[0]) for c in cases]
    if got == [c[1] for c in cases]:
        passed += 1
        print("PASS 1: sanitize_account đúng mọi case (trim/path-traversal/cap)")
    else:
        print(f"FAIL 1: sanitize_account cho {got}")

    # 2. state_path: None → quota.json; có label → quota-<slug>.json
    with tempfile.TemporaryDirectory(prefix="cici_acc_test_") as td:
        base = Path(td) / "quota.json"
        p_none = _quota.state_path(None, base)
        p_acc = _quota.state_path("acc1", base)
        p_san = _quota.state_path("../evil", base)
        if p_none == base and p_acc == base.with_name("quota-acc1.json") \
                and p_san == base.with_name("quota-.._evil.json"):
            passed += 1
            print("PASS 2: state_path đúng (legacy + per-account + sanitize)")
        else:
            print(f"FAIL 2: state_path cho {p_none}, {p_acc}, {p_san}")

    # ------------------------------------------------------------------ #
    # isolation giữa các account + legacy
    # ------------------------------------------------------------------ #
    # 3. record vào A/B riêng → count không lẫn; legacy file không bị đụng
    with tempfile.TemporaryDirectory(prefix="cici_acc_iso_") as td2:
        base2 = Path(td2) / "quota.json"
        sa = _quota.load_account("a", base2)
        _quota.record_success(sa, "image", now=1000.0)
        _quota.record_success(sa, "image", now=1001.0)
        _quota.save_account(sa, "a", base2)
        sb = _quota.load_account("b", base2)
        _quota.record_success(sb, "image", now=1002.0)
        _quota.save_account(sb, "b", base2)
        ca = _quota.count_recent(_quota.load_account("a", base2), "image", now=2000.0)
        cb = _quota.count_recent(_quota.load_account("b", base2), "image", now=2000.0)
        cleg = _quota.count_recent(_quota.load_account(None, base2), "image", now=2000.0)
        if ca == 2 and cb == 1 and cleg == 0 and not base2.exists():
            passed += 1
            print("PASS 3: quota A/B cách ly + legacy file không bị tạo")
        else:
            print(f"FAIL 3: ca={ca} cb={cb} cleg={cleg} legacy_exists={base2.exists()}")

    # 4. load_account file thiếu → fresh state (fail-open)
    with tempfile.TemporaryDirectory(prefix="cici_acc_fresh_") as td3:
        s4 = _quota.load_account("nope", Path(td3) / "quota.json")
        if _quota.count_recent(s4, "image", now=1.0) == 0:
            passed += 1
            print("PASS 4: load_account file thiếu → fresh state")
        else:
            print("FAIL 4: load_account missing không fresh")

    # 5. regression: load(path) positional vẫn dùng path như trước (stress test dùng)
    with tempfile.TemporaryDirectory(prefix="cici_acc_legacy_") as td4:
        p5 = Path(td4) / "q.json"
        st5 = _quota.QuotaState()
        _quota.record_success(st5, "image", now=500.0)
        _quota.save(st5, p5)
        loaded = _quota.load(p5)
        if _quota.count_recent(loaded, "image", now=600.0) == 1:
            passed += 1
            print("PASS 5: load(path) positional backward-compat")
        else:
            print("FAIL 5: load(path) positional sai")

    # ------------------------------------------------------------------ #
    # client payload / params
    # ------------------------------------------------------------------ #
    # 6. generate(account=...) → payload có account; 7. không có → không key
    _orig_client = _client.httpx.Client
    _client.httpx.Client = _FakeClient
    try:
        _FakeClient.calls = []
        _client.generate("p", "image", base="http://x", account="acc1")
        p6 = _FakeClient.calls[0][2]
        _FakeClient.calls = []
        _client.generate("p", "image", base="http://x")
        p7 = _FakeClient.calls[0][2]
        if p6.get("account") == "acc1" and "account" not in p7:
            passed += 1
            print("PASS 6: client generate truyền account trong payload + omit khi None")
        else:
            print(f"FAIL 6: payload acc={p6.get('account')!r}, none-case={p7}")

        # 7. quota(account=...) → query param; không có → params rỗng
        _FakeClient.calls = []
        _client.quota(account="acc1", base="http://x")
        q8 = _FakeClient.calls[0][2]
        _FakeClient.calls = []
        _client.quota(kind="image", base="http://x")
        q8b = _FakeClient.calls[0][2]
        if q8 == {"account": "acc1"} and q8b == {"kind": "image"}:
            passed += 1
            print("PASS 7: client quota truyền account param + giữ kind")
        else:
            print(f"FAIL 7: quota params {q8}, {q8b}")
    finally:
        _client.httpx.Client = _orig_client

    # ------------------------------------------------------------------ #
    # CLI plumb: _run_generation forward account tới api.generate
    # ------------------------------------------------------------------ #
    # 8. account được truyền xuyên suốt CLI → client
    import cici.cli as cli

    captured = {}

    def fake_generate(prompt, kind, base, timeout=10.0, model=None, references=None,
                      ratio=None, style=None, duration=None, account=None):
        captured["account"] = account
        return {"job_id": "mock-job", "timeout_s": 1}

    def fake_wait(*_a, **_kw):
        return {"status": "COMPLETED", "result_urls": []}

    _orig = (cli._preflight, _client.generate, _client.wait_status)
    cli._preflight = lambda base, auto_launch=True: True
    _client.generate = fake_generate
    _client.wait_status = fake_wait
    try:
        code8 = cli._run_generation("p", "image", False, "http://x", account="acc1")
        if code8 == _client.EXIT_OK and captured.get("account") == "acc1":
            passed += 1
            print("PASS 8: CLI _run_generation forward --account tới generate")
        else:
            print(f"FAIL 8: exit={code8} account={captured.get('account')!r}")
    finally:
        cli._preflight, _client.generate, _client.wait_status = _orig

    print(f"\n{passed}/8 tests passed")
    return 0 if passed == 8 else 1


if __name__ == "__main__":
    sys.exit(main())
