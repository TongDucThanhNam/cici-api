"""Auto-launch Cici app + core server when missing.

Tách khỏi cli.py cho dễ test. Logic:
  - _ensure_cici()   : nếu CDP port không trả lời → launch Cici.exe có CDP, chờ lên.
  - _ensure_server() : nếu core API không trả lời → spawn uvicorn ngầm (detached).
  - check_login()    : qua CDP xem Cici đã login ByteDance chưa.

Tất cả best-effort: làm được đến đâu làm, phần không được (login) báo người dùng.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

CDP_URL = "http://127.0.0.1:9222"
API_URL = "http://127.0.0.1:8000"

# Đường dẫn Cici (Windows). Override bằng env CICI_EXE nếu cài chỗ khác.
CICI_EXE_CANDIDATES = [
    os.environ.get("CICI_EXE"),
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "Cici" / "Application" / "app" / "Cici.exe"),
]
USER_DATA_CANDIDATES = [
    os.environ.get("CICI_USER_DATA"),
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "Cici" / "User Data"),
]


def _cdp_alive(timeout: float = 2.0) -> bool:
    try:
        r = httpx.get(f"{CDP_URL}/json/version", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _api_alive(timeout: float = 2.0) -> bool:
    try:
        r = httpx.get(f"{API_URL}/api/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _find_cici_exe() -> str | None:
    for c in CICI_EXE_CANDIDATES:
        if c and Path(c).exists():
            return c
    return None


def _find_user_data() -> str | None:
    for c in USER_DATA_CANDIDATES:
        if c and Path(c).exists():
            return c
    return None


def ensure_cici(log=print, cdp_timeout: float = 30.0) -> tuple[bool, str]:
    """Đảm bảo Cici chạy với CDP. Trả (ok, message).

    Nếu CDP đã up → ok ngay. Nếu chưa → launch Cici.exe + chờ CDP lên.
    Không kill instance cũ (tránh làm mất phiên nếu người dùng đang dùng).
    """
    if _cdp_alive():
        return True, "Cici CDP đã chạy."

    exe = _find_cici_exe()
    if not exe:
        return False, (
            "Không tìm thấy Cici.exe. Cài Cici/Dola Browser, hoặc set env "
            "CICI_EXE=<đường dẫn Cici.exe>."
        )
    if sys.platform == "win32":
        import subprocess
        ud = _find_user_data()
        args = [exe, "--remote-debugging-port=9222"]
        if ud:
            args.append(f"--user-data-dir={ud}")
        # detached: Cici sống độc lập với CLI process
        subprocess.Popen(args, close_fds=True, creationflags=0x00000008)  # DETACHED_PROCESS
        log(f"[dim]Đang khởi động Cici: {exe}[/]")
    else:
        return False, (
            "Auto-launch Cici chỉ hỗ trợ Windows. Trên macOS/Linux hãy mở Cici "
            "thủ công với flag --remote-debugging-port=9222."
        )

    # chờ CDP lên
    deadline = time.time() + cdp_timeout
    while time.time() < deadline:
        time.sleep(1.5)
        if _cdp_alive():
            return True, f"Cici đã khởi động (sau ~{int(time.time()+cdp_timeout-deadline)}s)."
    return False, (
        "Cici khởi động nhưng CDP không lên sau "
        f"{int(cdp_timeout)}s. Có thể Cici đang update hoặc crash."
    )


def ensure_server(log=print, cwd: str | None = None, api_timeout: float = 20.0) -> tuple[bool, str]:
    """Đảm bảo core API server chạy. Trả (ok, message).

    Server là module self-contained trong package: spawn `python -m cici.server`
    (không cần folder repo), log ra ~/.cici/server.log. `cwd` giữ lại cho
    backward-compat nhưng không còn bắt buộc.
    """
    if _api_alive():
        return True, "Core server đã chạy."

    if sys.platform != "win32":
        return False, (
            "Auto-spawn server chỉ hỗ trợ Windows. Chạy `python -m cici.server` thủ công."
        )
    import subprocess

    log_dir = Path.home() / ".cici"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "server.log"
    log(f"[dim]Đang khởi động core server (log: {log_path})[/]")
    log_fd = open(log_path, "a", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, "-m", "cici.server", "--host", "127.0.0.1", "--port", "8000"],
        cwd=cwd,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        close_fds=True,
        creationflags=0x00000008,  # DETACHED_PROCESS
    )

    deadline = time.time() + api_timeout
    while time.time() < deadline:
        time.sleep(1.0)
        if _api_alive():
            return True, f"Core server đã khởi động (log: {log_path})."
    return False, f"Core server không lên sau {int(api_timeout)}s. Xem log: {log_path}"


def check_login(timeout: float = 5.0) -> tuple[bool, str]:
    """Qua CDP xem Cici đã login ByteDance chưa.

    Heuristic: query CDP /json, tìm chat page, kiểm tra title/url không phải guest.
    Trả (logged_in, detail). Best-effort — false negative có thể.
    """
    try:
        r = httpx.get(f"{CDP_URL}/json", timeout=timeout)
        tabs = r.json()
    except Exception as e:
        return False, f"không đọc được CDP tabs: {e}"

    chat_tabs = [t for t in tabs if "dola-chat" in t.get("url", "") or "dola" in t.get("url", "")]
    if not chat_tabs:
        return False, "không tìm thấy tab chat Cici."
    # Cici khi chưa login thường redirect về trang login hoặc title = "Dola" mà url có /login
    sample = chat_tabs[0]
    url = sample.get("url", "")
    title = sample.get("title", "")
    if "/login" in url or "login" in title.lower():
        return False, "Cici đang ở trang login."
    # Heuristic thêm: nếu title chỉ là generic "Dola" mà không có conversation → có thể guest
    return True, f"Cici có vẻ đã login (tab: {url})."