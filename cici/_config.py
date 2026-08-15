"""Config resolution cho bản cài self-contained (pip/pipx).

Thứ tự ưu tiên:
  1. env CICI_CONFIG=<đường dẫn>
  2. ./config.yaml trong CWD (workflow dev: chạy từ repo root)
  3. ~/.cici/config.yaml — bản copy user-editable (server tự tạo lần đầu)
  4. config.yaml đóng gói trong package (fallback read-only)

Pure logic — không import FastAPI/CLI để test dễ.
"""
from __future__ import annotations

import os
import shutil
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


def ensure_user_copy() -> Path:
    """Copy config đóng gói ra ~/.cici/config.yaml nếu chưa có (để user chỉnh
    selector/timing mà không phải đụng site-packages). Trả path sẽ dùng."""
    if not USER_CONFIG.exists() and PACKAGED_CONFIG.exists():
        try:
            USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PACKAGED_CONFIG, USER_CONFIG)
        except OSError:
            pass  # read-only home / portable install — dùng packaged config
    return config_path()


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else ensure_user_copy()
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
