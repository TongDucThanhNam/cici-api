"""Deterministic tests cho đa-provider (cici + doubao).

Không cần app/mạng (fake httpx + config thật trong repo) — hand-rolled script:

    python tests/test_provider.py

Phạm vi: registry resolution theo provider, cdp overlay, quota state path
namespace, client payload, CLI plumbing, server-side section resolution.
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

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

    def get(self, url, params=None):
        _FakeClient.calls.append(("GET", url, params))
        return _FakeResp({})


CFG = yaml.safe_load(open(Path(__file__).resolve().parent.parent / "config.yaml",
                          encoding="utf-8"))


def main() -> int:
    passed = 0

    # ------------------------------------------------------------------ #
    # quota state path: cici giữ legacy, doubao namespace riêng
    # ------------------------------------------------------------------ #
    with tempfile.TemporaryDirectory(prefix="cici_prov_q_") as td:
        base = Path(td) / "quota.json"
        cici_none = _quota.state_path(None, base, "cici")
        cici_acc = _quota.state_path("a", base, "cici")
        db_none = _quota.state_path(None, base, "doubao")
        db_acc = _quota.state_path("a", base, "doubao")
        if (cici_none.name == "quota.json" and cici_acc.name == "quota-a.json"
                and db_none.name == "quota-doubao.json"
                and db_acc.name == "quota-doubao-a.json"):
            passed += 1
            print("PASS 1: quota state path — cici legacy, doubao namespace riêng")
        else:
            print(f"FAIL 1: {[p.name for p in (cici_none, cici_acc, db_none, db_acc)]}")

        # 2. isolation: record vào doubao không dính file cici
        sd = _quota.load_account(None, base, "doubao")
        _quota.record_success(sd, "image", now=1000.0)
        _quota.save_account(sd, None, base, "doubao")
        sc = _quota.load_account(None, base, "cici")
        if (_quota.count_recent(sd, "image", now=1100.0) == 1
                and _quota.count_recent(sc, "image", now=1100.0) == 0
                and not base.exists()):
            passed += 1
            print("PASS 2: quota doubao/cici cách ly hoàn toàn (file riêng)")
        else:
            print("FAIL 2: doubao count leaked sang cici?")

    # ------------------------------------------------------------------ #
    # client payload / params
    # ------------------------------------------------------------------ #
    _orig = _client.httpx.Client
    _client.httpx.Client = _FakeClient
    try:
        _FakeClient.calls = []
        _client.generate("p", "image", base="http://x", provider="doubao")
        p3 = _FakeClient.calls[0][2]
        _FakeClient.calls = []
        _client.generate("p", "image", base="http://x")
        p3b = _FakeClient.calls[0][2]
        if p3.get("provider") == "doubao" and "provider" not in p3b:
            passed += 1
            print("PASS 3: client generate — provider trong payload cho doubao, omit cho cici")
        else:
            print(f"FAIL 3: {p3} / {p3b}")

        _FakeClient.calls = []
        _client.models(base="http://x", provider="doubao")
        m4 = _FakeClient.calls[0][2]
        _FakeClient.calls = []
        _client.quota(base="http://x", provider="doubao")
        q4 = _FakeClient.calls[0][2]
        if m4 == {"provider": "doubao"} and q4 == {"provider": "doubao"}:
            passed += 1
            print("PASS 4: client models/quota truyền ?provider=doubao")
        else:
            print(f"FAIL 4: models={m4} quota={q4}")
    finally:
        _client.httpx.Client = _orig

    # ------------------------------------------------------------------ #
    # driver: cdp overlay + registry theo provider + has_text exact
    # ------------------------------------------------------------------ #
    from cici.driver import CiciDriver

    drv = CiciDriver(CFG)
    cdp_cici = drv._cdp_for("cici")
    cdp_db = drv._cdp_for("doubao")
    ok5 = (cdp_cici["endpoint"].endswith("9222")
           and "dola-chat/chat" in cdp_cici["chat_url_pattern"]
           and cdp_db["endpoint"].endswith("9223")
           and "doubao-chat/chat" in cdp_db["chat_url_pattern"]
           and cdp_db["create_image_url"].startswith("chrome://doubao-chat/")
           and cdp_db["connect_timeout"] == cdp_cici["connect_timeout"])  # base kế thừa
    if ok5:
        passed += 1
        print("PASS 5: cdp overlay — doubao endpoint/URL riêng, timing base dùng chung")
    else:
        print(f"FAIL 5: cici={cdp_cici.get('endpoint')} db={cdp_db.get('endpoint')}")

    drv._current_provider = "cici"
    m_cici = drv._resolve_model("image", None)
    drv._current_provider = "doubao"
    m_db = drv._resolve_model("image", None)
    r_db = drv._resolve_option("image", "ratios", "auto")
    if (m_cici["alias"] == "seedream-5-pro"
            and m_db["alias"] == "seedream-4.5"
            and r_db["select_text"] == "自动"):
        passed += 1
        print("PASS 6: registry theo provider — default + alias resolve đúng")
    else:
        print(f"FAIL 6: cici={m_cici['alias']} db={m_db['alias']} auto={r_db}")

    # 7. _has_text exact: neo ^$ — "16:9" không khớp "比例 16:9"
    pat = drv._has_text("16:9", exact=True)
    loose = drv._has_text("16:9")
    multi = drv._has_text(["自动", "Auto"], exact=True)
    if (pat.search("16:9") and not pat.search("比例 16:9")
            and "16:9" in loose  # string passthrough khi không list/exact
            and multi.search("自动") and multi.search("Auto")
            and not multi.search("Auto · 10s")):
        passed += 1
        print("PASS 7: _has_text exact neo ^$ (dùng cho get_by_role name)")
    else:
        print(f"FAIL 7: pat={pat} loose={loose!r} multi={multi}")

    # ------------------------------------------------------------------ #
    # CLI plumbing: _run_generation forward provider tới api.generate
    # ------------------------------------------------------------------ #
    import cici.cli as cli

    captured = {}

    def fake_generate(prompt, kind, base, timeout=10.0, model=None, references=None,
                      ratio=None, style=None, duration=None, account=None,
                      provider="cici"):
        captured["provider"] = provider
        return {"job_id": "mock-job", "timeout_s": 1}

    def fake_wait(*_a, **_kw):
        return {"status": "COMPLETED", "result_urls": []}

    _orig2 = (cli._preflight, _client.generate, _client.wait_status)
    cli._preflight = lambda base, auto_launch=True, provider="cici": True
    _client.generate = fake_generate
    _client.wait_status = fake_wait
    try:
        code8 = cli._run_generation("p", "image", False, "http://x", provider="doubao")
        if code8 == _client.EXIT_OK and captured.get("provider") == "doubao":
            passed += 1
            print("PASS 8: CLI _run_generation forward --provider tới generate")
        else:
            print(f"FAIL 8: code={code8} provider={captured.get('provider')!r}")
    finally:
        cli._preflight, _client.generate, _client.wait_status = _orig2

    # ------------------------------------------------------------------ #
    # server: section resolution theo provider
    # ------------------------------------------------------------------ #
    from cici import server

    models_db = server._provider_section("models", "doubao")
    models_cici = server._provider_section("models", "cici")
    opts_db = server._provider_section("options", "doubao")
    ok9 = ("image" in models_db and "video" in models_db
           and models_db is not models_cici
           and "image" in models_cici
           and len(opts_db["image"]["styles"]) >= 30)
    if ok9:
        passed += 1
        print("PASS 9: server _provider_section — doubao registry riêng, cici legacy")
    else:
        print(f"FAIL 9: db_keys={list(models_db)[:3]} cici_keys={list(models_cici)[:3]}")

    # ------------------------------------------------------------------ #
    # alt-provider hint khi quota cạn
    # ------------------------------------------------------------------ #
    from cici import _launcher

    _orig3 = (_launcher._providers_cfg, _launcher._find_app_exe, _launcher._cdp_alive)
    _launcher._providers_cfg = lambda: {"cici": {"label": "Cici (Dola)", "cdp_port": 9222},
                                        "doubao": {"label": "Doubao (豆包)", "cdp_port": 9223}}
    _launcher._cdp_alive = lambda endpoint, timeout=2.0: False
    try:
        # cài sẵn cả hai app → hint hai chiều đều có
        _launcher._find_app_exe = lambda p: f"C:/fake/{p}.exe"
        import cici.cli as cli2
        h10 = cli2._alt_provider_hint("cici")
        ok10 = (h10 is not None and "--provider doubao" in h10
                and "RIÊNG" in h10)
        if ok10:
            passed += 1
            print("PASS 10: quota cạn cici → gợi ý --provider doubao (khi Doubao available)")
        else:
            print(f"FAIL 10: {h10!r}")

        h11 = cli2._alt_provider_hint("doubao")
        if h11 is not None and "--provider cici" in h11:
            passed += 1
            print("PASS 11: quota cạn doubao → gợi ý ngược --provider cici")
        else:
            print(f"FAIL 11: {h11!r}")

        _launcher._find_app_exe = lambda p: "C:/fake/Cici.exe" if p == "cici" else None
        # doubao không exe + không CDP (cdp_alive đã False) → không gợi ý gì
        h12 = cli2._alt_provider_hint("cici")
        if h12 is None:
            passed += 1
            print("PASS 12: không có provider thay thế → None (không hint ảo)")
        else:
            print(f"FAIL 12: {h12!r}")
    finally:
        _launcher._providers_cfg, _launcher._find_app_exe, _launcher._cdp_alive = _orig3

    # 13. full-size marker theo provider (doubao: i_pre_wm qua network,
    # cici: image_pre_watermark qua DOM viewer)
    drv._current_provider = "cici"
    m_cici = drv._fullsize_marker()
    drv._current_provider = "doubao"
    m_db = drv._fullsize_marker()
    if m_cici == "image_pre_watermark" and m_db == "i_pre_wm":
        passed += 1
        print("PASS 13: full-size marker theo provider (cici=pre_watermark, doubao=i_pre_wm)")
    else:
        print(f"FAIL 13: cici={m_cici!r} doubao={m_db!r}")

    print(f"\n{passed}/13 tests passed")
    return 0 if passed == 13 else 1


if __name__ == "__main__":
    sys.exit(main())
