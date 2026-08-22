"""Deterministic tests cho watermark-free video capture (không cần app/quota).

    python tests/test_watermark_video.py

Phạm vi:
  - _parse_watermark_free_video: các shape response get_without_watermark
    (download_url str/list, video_model fallback, entitlement tắt, input rác).
  - _extract_video_resource_keys: suy ra vid/uri từ URL video CDN thật.
  - CiciDriver._capture_watermark_free_video: các nhánh skip best-effort
    (URL rỗng, provider ngoài allowlist, thiếu host) — trả nguyên URL gốc,
    không đụng Playwright/CDP.
  - config.yaml (cả 2 bản) có đủ key mới và video_timeout đã nâng.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cici.driver import (  # noqa: E402
    CiciDriver,
    _extract_video_resource_keys,
    _parse_watermark_free_video,
)


# ---- response fixtures (shape verify live 2026-08-22, build 147.0.7727.149) -- #
FIX_STR = {
    "code": 0, "msg": "",
    "data": {
        "without_watermark": True,
        "download_video": {
            "v1": {"download_url": "https://cdn/clean1.mp4"},
        },
        "preview_video": {"v1": {"download_url": "https://cdn/prev1.mp4"}},
    },
}
FIX_LIST = {
    "code": 0,
    "data": {
        "download_video": {
            "v1": {"download_url": ["https://cdn/a.mp4", "https://cdn/b.mp4"]},
            "v2": {"download_url": "https://cdn/c.mp4"},
        },
    },
}
FIX_MODEL = {
    "code": 0,
    "data": {
        "download_video": {
            "v1": {"video_model": [
                {"main_url": "https://cdn/m1.mp4", "backup_url": "x"},
                {"url": "https://cdn/m2.mp4"},
            ]},
        },
    },
}
# Verify live: account không có entitlement → không có download_video
FIX_NO_ENTITLEMENT = {"code": 0, "data": {"without_watermark": False}}
FIX_ERROR = {"code": 710010202, "msg": "system error", "data": {}}

# URL video thật (đã soi live, mã hoá phần hash dẫn đầu)
REAL_VIDEO_URL = (
    "https://v16-dola.dola.com/97f72e1cb98a502b752285ffd28791e8/6a9269f9"
    "/video/tos/mya/tos-mya-ve-50851/oYixyBES31DDNfwc2aTXFbSFfgYgEqIqWhQvu3/"
    "?a=489823&ch=0&lr=cici_ai&mime_type=video_mp4"
)


def _driver(providers=None, hosts=None) -> CiciDriver:
    sel = {
        "message_list": "x", "bot_message": "x", "result_video": "x",
        "video_watermark_api_hosts": hosts if hosts is not None
        else {"cici": "www.dola.com", "doubao": "www.doubao.com"},
    }
    if providers is not None:
        sel["video_watermark_providers"] = providers
    return CiciDriver({"selectors": sel, "timing": {"video_watermark_wait": 1}})


def main() -> int:
    passed = 0

    # 1. download_url dạng chuỗi — lấy đúng URL sạch, bỏ preview
    got = _parse_watermark_free_video(FIX_STR)
    assert got == ["https://cdn/clean1.mp4"], got
    passed += 1
    print("PASS 1: download_url string extracted, preview ignored")

    # 2. download_url dạng list + nhiều vid — giữ thứ tự
    got = _parse_watermark_free_video(FIX_LIST)
    assert got == ["https://cdn/a.mp4", "https://cdn/b.mp4", "https://cdn/c.mp4"], got
    passed += 1
    print("PASS 2: download_url list across vids in order")

    # 3. fallback video_model (main_url / url)
    got = _parse_watermark_free_video(FIX_MODEL)
    assert got == ["https://cdn/m1.mp4", "https://cdn/m2.mp4"], got
    passed += 1
    print("PASS 3: video_model fallback (main_url/url)")

    # 4. entitlement tắt / lỗi API / thiếu data → rỗng (giữ URL gốc phía driver)
    for fx in (FIX_NO_ENTITLEMENT, FIX_ERROR, {}, {"data": None}, None, [1, 2]):
        assert _parse_watermark_free_video(fx) == [], fx
    passed += 1
    print("PASS 4: no-entitlement / error / junk payloads yield empty list")

    # 5. raw JSON text (như kết quả _WATERMARK_FETCH_JS)
    import json as _json
    got = _parse_watermark_free_video(_json.dumps(FIX_STR))
    assert got == ["https://cdn/clean1.mp4"], got
    assert _parse_watermark_free_video("not json") == []
    passed += 1
    print("PASS 5: raw JSON text accepted, garbage text rejected")

    # 6. suy vid/uri từ URL video CDN thật
    vids, uris = _extract_video_resource_keys([REAL_VIDEO_URL])
    assert vids == ["oYixyBES31DDNfwc2aTXFbSFfgYgEqIqWhQvu3"], vids
    assert uris == ["tos-mya-ve-50851/oYixyBES31DDNfwc2aTXFbSFfgYgEqIqWhQvu3"], uris
    # URL không phải video CDN / trùng key → bỏ qua, khử trùng
    vids2, uris2 = _extract_video_resource_keys(
        [REAL_VIDEO_URL, REAL_VIDEO_URL.split("?")[0], "https://example.com/x.mp4", ""])
    assert vids2 == vids and uris2 == uris, (vids2, uris2)
    assert _extract_video_resource_keys(["https://example.com/a"]) == ([], [])
    passed += 1
    print("PASS 6: vid/uri extraction from CDN URLs (dedup, non-video ignored)")

    # 7. skip paths của capture — trả nguyên URL gốc, không đụng CDP
    urls = ["https://v16-dola.dola.com/wm.mp4"]
    assert asyncio.run(_driver()._capture_watermark_free_video([])) == []
    d = _driver()
    d._current_provider = "doubao"
    assert asyncio.run(d._capture_watermark_free_video(urls)) == urls
    assert asyncio.run(_driver(hosts={})._capture_watermark_free_video(urls)) == urls
    passed += 1
    print("PASS 7: capture skips (empty / provider / missing host) return originals")

    # 8. config đồng bộ 2 bản: key mới + timeout/version đã nâng
    root = Path(__file__).resolve().parent.parent
    for rel in ("config.yaml", "cici/config.yaml"):
        cfg = yaml.safe_load((root / rel).read_text(encoding="utf-8"))
        sel = cfg["selectors"]
        assert "cici" in (sel.get("video_watermark_providers") or []), rel
        hosts = sel.get("video_watermark_api_hosts") or {}
        assert hosts.get("cici") == "www.dola.com", rel
        assert hosts.get("doubao") == "www.doubao.com", rel
        assert cfg["timing"]["video_watermark_wait"] == 30, rel
        assert cfg["timing"]["video_timeout"] == 1800, rel
        assert cfg["config_version"] >= 5, rel
    passed += 1
    print("PASS 8: both config.yaml copies carry new keys, video_timeout=1800, version>=5")

    print(f"\n{passed}/8 tests passed")
    return 0 if passed == 8 else 1


if __name__ == "__main__":
    sys.exit(main())
