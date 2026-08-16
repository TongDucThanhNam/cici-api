"""Deterministic tests cho auto-upgrade config user copy (~/.cici/config.yaml).

Regression: server từng mãi mãi đọc config cũ trong ~/.cici/ (shadow repo/
package) sau khi tool update selector/registry — user phải sync tay.

    python tests/test_config_upgrade.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cici import _config  # noqa: E402

V2 = "config_version: 2\nproviders:\n  doubao:\n    cdp_port: 9223\n"
V2_OLDER = "# old config, no version key (pre-upgrade-mechanism)\nfoo: bar\n"
V3 = "config_version: 3\nfuture: true\n"


def run_isolated(user_content: str | None, packaged_content: str):
    """Chạy ensure_user_copy với USER/PACKAGED trỏ vào temp dir. Trả
    (nội dung user copy sau khi chạy, list file backup)."""
    with tempfile.TemporaryDirectory(prefix="cici_cfg_up_") as td:
        tdp = Path(td)
        packaged = tdp / "packaged.yaml"
        packaged.write_text(packaged_content, encoding="utf-8")
        user = tdp / "home" / ".cici" / "config.yaml"
        if user_content is not None:
            user.parent.mkdir(parents=True)
            user.write_text(user_content, encoding="utf-8")
        orig = (_config.USER_CONFIG, _config.PACKAGED_CONFIG)
        _config.USER_CONFIG, _config.PACKAGED_CONFIG = user, packaged
        try:
            _config.ensure_user_copy()
            after = user.read_text(encoding="utf-8") if user.exists() else None
            backups = sorted(p.name for p in user.parent.glob("config.yaml.bak-*"))
            return after, backups
        finally:
            _config.USER_CONFIG, _config.PACKAGED_CONFIG = orig


def main() -> int:
    passed = 0

    # 1. chưa có user copy → tạo từ packaged
    after, bak = run_isolated(None, V2)
    if after == V2 and bak == []:
        passed += 1
        print("PASS 1: thiếu user copy → tạo từ packaged, không backup")
    else:
        print(f"FAIL 1: after={after!r} bak={bak}")

    # 2. user copy cũ (không có version key) + packaged v2 → backup + nâng cấp
    after, bak = run_isolated(V2_OLDER, V2)
    if after == V2 and len(bak) == 1:
        passed += 1
        print("PASS 2: user copy cũ hơn → backup .bak-<ts> rồi nâng cấp")
    else:
        print(f"FAIL 2: after={after!r} bak={bak}")

    # 3. cùng version → KHÔNG đụng (giữ edit tay)
    custom = "config_version: 2\nmy_custom_selector: 'edited'\n"
    after, bak = run_isolated(custom, V2)
    if after == custom and bak == []:
        passed += 1
        print("PASS 3: cùng version → không đụng bản user (edit tay giữ nguyên)")
    else:
        print(f"FAIL 3: after={after!r} bak={bak}")

    # 4. user version CAO hơn packaged → không downgrade
    after, bak = run_isolated(V3, V2)
    if after == V3 and bak == []:
        passed += 1
        print("PASS 4: user version cao hơn → không tự downgrade")
    else:
        print(f"FAIL 4: after={after!r} bak={bak}")

    print(f"\n{passed}/4 tests passed")
    return 0 if passed == 4 else 1


if __name__ == "__main__":
    sys.exit(main())
