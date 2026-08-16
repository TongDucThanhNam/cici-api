"""Deterministic tests cho prompt handling: không cap độ dài + 422 rendering.

Không cần server/mạng (fake httpx + monkeypatch CLI) — hand-rolled script:

    python tests/test_prompt_validation.py

Regression: prompt > 2000 ký tự từng bị reject ở client lẫn server (max_length
giờ đã bỏ — Cici không giới hạn), và 422 detail là list Pydantic từng bị dump
thô kèm hint sai ("Chạy `cici models` để xem alias hợp lệ").
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cici import _client  # noqa: E402


class _FakeResp:
    def __init__(self, data=None, status_code=200):
        self._data = data or {}
        self.status_code = status_code

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


def main() -> int:
    passed = 0

    # ------------------------------------------------------------------ #
    # client: không còn cap độ dài
    # ------------------------------------------------------------------ #
    # 1. prompt dài (kể cả > 2000) vẫn được POST nguyên vẹn
    _orig_client = _client.httpx.Client
    _client.httpx.Client = _FakeClient
    try:
        for n in (2000, 2001, 5000, 50000):
            _FakeClient.calls = []
            _client.generate("x" * n, "image", base="http://x")
            sent = _FakeClient.calls[0][2].get("prompt") if _FakeClient.calls else None
            if len(_FakeClient.calls) == 1 and sent == "x" * n:
                passed += 1
                print(f"PASS: client POST nguyên vẹn prompt {n} ký tự (không cap)")
            else:
                print(f"FAIL: n={n} calls={len(_FakeClient.calls)} len_sent={len(sent or '')}")

        # 2. 422 detail là list Pydantic (FastAPI schema reject, vd references
        #    quá hạn mức) → message đọc được, không phải list repr
        class _Fake422Client(_FakeClient):
            def post(self, url, json):
                _Fake422Client.calls.append(("POST", url, json))
                return _FakeResp(
                    {"detail": [{"type": "string_too_long",
                                 "loc": ["body", "prompt"],
                                 "msg": "String should have at most 2000 characters",
                                 "ctx": {"max_length": 2000}}]},
                    status_code=422)

        _client.httpx.Client = _Fake422Client
        try:
            _Fake422Client.calls = []
            _client.generate("y" * 10, "image", base="http://x")
            err2 = None
        except ValueError as e:
            err2 = str(e)
        if err2 is not None and not err2.startswith("[") \
                and "prompt" in err2 and "2000" in err2:
            passed += 1
            print(f"PASS: 422 list detail nén thành string đọc được ({err2!r})")
        else:
            print(f"FAIL: err={err2!r}")
    finally:
        _client.httpx.Client = _orig_client

    # ------------------------------------------------------------------ #
    # CLI: prompt dài vẫn chạy + hint chỉ hiện cho lỗi alias
    # ------------------------------------------------------------------ #
    import cici.cli as cli

    # 3. prompt 5000 ký tự: KHÔNG bị chặn trước preflight, generate được gọi
    captured = {}

    def fake_generate(prompt, kind, base, timeout=10.0, model=None, references=None,
                      ratio=None, style=None, duration=None, account=None, provider="cici"):
        captured["prompt"] = prompt
        return {"job_id": "mock-job", "timeout_s": 1}

    def fake_wait(*_a, **_kw):
        return {"status": "COMPLETED", "result_urls": []}

    _orig = (cli._preflight, _client.generate, _client.wait_status, cli._emit_json)
    cli._preflight = lambda base, auto_launch=True, provider="cici": True
    _client.generate = fake_generate
    _client.wait_status = fake_wait

    emitted: list[dict] = []
    cli._emit_json = emitted.append
    try:
        code3 = cli._run_generation("z" * 5000, "image", True, "http://x")
        if code3 == _client.EXIT_OK and captured.get("prompt") == "z" * 5000:
            passed += 1
            print("PASS: CLI _run_generation chạy xuyên suốt với prompt 5000 ký tự")
        else:
            print(f"FAIL: code={code3} captured_len={len(captured.get('prompt', ''))}")

        # 4. ValueError "Unknown model ..." → JSON vẫn có hint alias
        def unknown_model(*_a, **_kw):
            raise ValueError("Unknown model 'x' for type 'image'. Valid: ['a']")

        _client.generate = unknown_model
        emitted.clear()
        code4 = cli._run_generation("ok", "image", True, "http://x")
        payload4 = emitted[-1] if emitted else {}
        if code4 == _client.EXIT_FAILED and "alias hợp lệ" in payload4.get("hint", ""):
            passed += 1
            print("PASS: lỗi unknown model vẫn hiện hint alias trong JSON")
        else:
            print(f"FAIL: code={code4} payload={payload4}")

        # 5. ValueError prompt (vd rỗng) → KHÔNG hint alias gây hiểu lầm
        def bad_prompt(*_a, **_kw):
            raise ValueError("Prompt không được rỗng hoặc chỉ khoảng trắng")

        _client.generate = bad_prompt
        emitted.clear()
        code5 = cli._run_generation(" ", "image", True, "http://x")
        payload5 = emitted[-1] if emitted else {}
        if code5 == _client.EXIT_FAILED and "hint" not in payload5:
            passed += 1
            print("PASS: lỗi prompt không có hint alias")
        else:
            print(f"FAIL: code={code5} payload={payload5}")
    finally:
        cli._preflight, _client.generate, _client.wait_status, cli._emit_json = _orig

    print(f"\n{passed}/8 tests passed")
    return 0 if passed == 8 else 1


if __name__ == "__main__":
    sys.exit(main())
