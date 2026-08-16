"""Config resolution cho bản cài self-contained (pip/pipx).

Thứ tự ưu tiên:
  1. env CICI_CONFIG=<đường dẫn>
  2. ./config.yaml trong CWD (workflow dev: chạy từ repo root)
  3. ~/.cici/config.yaml — bản copy user-editable (tự tạo + TỰ NÂNG CẤP khi
     packaged config có config_version cao hơn, backup bản cũ)
  4. config.yaml đóng gói trong package (fallback read-only)

Pure logic — không import FastAPI/CLI để test dễ.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import yaml

PACKAGED_CONFIG = Path(__file__).resolve().parent / "config.yaml"
USER_CONFIG = Path.home() / ".cici" / "config.yaml"


def config_path() -> Path:
    """Đường dẫn config sẽ được dùng (không tạo file)."""
    env = os.environ.get("CICI_CONFIG")
    if env:
        return Path(env)
    cwd_cfg = Path.cwd() / "config.yaml"
    if cwd_cfg.exists():
        return cwd_cfg
    if USER_CONFIG.exists():
        return USER_CONFIG
    return PACKAGED_CONFIG


def _config_version(path: Path) -> int:
    """Đọc config_version của file (0 nếu thiếu/đọc lỗi — config cũ ship
    trước khi có cơ chế này)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        v = data.get("config_version", 0)
        return int(v) if isinstance(v, (int, float)) else 0
    except Exception:  # noqa: BLE001 — config hỏng → coi như version 0
        return 0


def ensure_user_copy() -> Path:
    """Copy config đóng gói ra ~/.cici/config.yaml (để user chỉnh
    selector/timing mà không phải đụng site-packages).

    TỰ NÂNG CẤP: khi packaged config có `config_version` CAO HƠN bản user
    (tool update selector/registry mới) thì backup bản cũ thành
    config.yaml.bak-<timestamp> rồi đè bằng bản mới — tránh trạng thái
    "server mãi mãi đọc config cũ shadow trong ~/.cici/". Bản user cùng
    version hoặc mới hơn thì KHÔNG đụng (tôn trọng edit tay).
    """
    try:
        if PACKAGED_CONFIG.exists():
            USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            if not USER_CONFIG.exists():
                shutil.copyfile(PACKAGED_CONFIG, USER_CONFIG)
            elif _config_version(USER_CONFIG) < _config_version(PACKAGED_CONFIG):
                backup = USER_CONFIG.with_name(
                    f"config.yaml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
                shutil.copyfile(USER_CONFIG, backup)
                shutil.copyfile(PACKAGED_CONFIG, USER_CONFIG)
    except OSError:
        pass  # read-only home / portable install — dùng packaged config
    return config_path()


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else ensure_user_copy()
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
