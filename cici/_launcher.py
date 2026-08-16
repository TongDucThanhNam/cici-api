"""Auto-launch Cici app + core server when missing.

Tách khỏi cli.py cho dễ test. Logic:
  - ensure_app()    : nếu CDP port của provider không trả lời → launch exe có
                      CDP, chờ lên (Cici 9222 / Doubao 9223 — xem config.yaml
                      providers.<name>).
  - _ensure_server() : nếu core API không trả lời → spawn uvicorn ngầm (detached).
  - check_login()    : qua CDP xem app đã login ByteDance chưa.

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

# Fallback khi không đọc được config (providers.<name> trong config.yaml là
# nguồn chân lý — đây chỉ là default cho cici).
_DEFAULT_PROVIDERS = {
    "cici": {
        "label": "Cici (Dola)",
        "exe_env": "CICI_EXE",
        "exe_candidates": ["Cici/Application/app/Cici.exe"],
        "cdp_port": 9222,
        "chat_host": "dola",
    },
    "doubao": {
        "label": "Doubao (豆包)",
        "exe_env": "DOUBAO_EXE",
        # STUB ở gốc — bắt buộc: launch app/Doubao.exe trực tiếp sẽ bị lờ flag CDP
        "exe_candidates": ["Doubao/Application/Doubao.exe"],
        "cdp_port": 9223,
        "chat_host": "doubao",
    },
}


def _providers_cfg() -> dict:
    try:
        from cici import _config
        cfg = _config.load_config().get("providers")
        if cfg:
            merged = dict(_DEFAULT_PROVIDERS)
            merged.update(cfg)
            return merged
    except Exception:  # noqa: BLE001 — config hỏng thì dùng default
        pass
    return _DEFAULT_PROVIDERS


def _provider_cfg(provider: str) -> dict:
    provs = _providers_cfg()
    if provider not in provs:
        raise ValueError(
            f"Unknown provider '{provider}'. Valid: {sorted(provs)}")
    return provs[provider]


def _cdp_endpoint(provider: str = "cici") -> str:
    return f"http://127.0.0.1:{_provider_cfg(provider)['cdp_port']}"


def _cdp_alive(endpoint: str = CDP_URL, timeout: float = 2.0) -> bool:
    try:
        r = httpx.get(f"{endpoint}/json/version", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _api_alive(timeout: float = 2.0) -> bool:
    try:
        r = httpx.get(f"{API_URL}/api/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _find_app_exe(provider: str) -> str | None:
    prov = _provider_cfg(provider)
    env = os.environ.get(prov.get("exe_env", ""))
    candidates = [env] if env else []
    candidates += [
        str(Path(os.environ.get("LOCALAPPDATA", "")) / rel)
        for rel in prov.get("exe_candidates", [])
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def ensure_app(provider: str = "cici", log=print,
               cdp_timeout: float = 30.0) -> tuple[bool, str]:
    """Đảm bảo app của provider chạy với CDP. Trả (ok, message).

    Nếu CDP đã up → ok ngay. Nếu chưa → launch exe + chờ CDP lên.
    Không kill instance cũ (tránh làm mất phiên nếu người dùng đang dùng).
    """
    prov = _provider_cfg(provider)
    endpoint = _cdp_endpoint(provider)
    port = prov["cdp_port"]
    label = prov.get("label", provider)

    if _cdp_alive(endpoint):
        return True, f"{label} CDP đã chạy."

    exe = _find_app_exe(provider)
    if not exe:
        return False, (
            f"Không tìm thấy exe của {label}. Cài app, hoặc set env "
            f"{prov.get('exe_env')}=<đường dẫn exe>."
        )
    if sys.platform == "win32":
        import subprocess
        args = [exe, f"--remote-debugging-port={port}"]
        # user-data-dir: giữ profile mặc định của app nếu tìm thấy
        app_name = Path(exe).parts[-3] if len(Path(exe).parts) >= 3 else None
        if app_name:
            ud = Path(os.environ.get("LOCALAPPDATA", "")) / app_name / "User Data"
            if ud.exists():
                args.append(f"--user-data-dir={ud}")
        # detached: app sống độc lập với CLI process
        subprocess.Popen(args, close_fds=True, creationflags=0x00000008)  # DETACHED_PROCESS
        log(f"[dim]Đang khởi động {label}: {exe}[/]")
    else:
        return False, (
            f"Auto-launch {label} chỉ hỗ trợ Windows. Trên macOS/Linux hãy mở "
            f"app thủ công với flag --remote-debugging-port={port}."
        )

    # chờ CDP lên
    deadline = time.time() + cdp_timeout
    while time.time() < deadline:
        time.sleep(1.5)
        if _cdp_alive(endpoint):
            return True, f"{label} đã khởi động (sau ~{int(time.time()+cdp_timeout-deadline)}s)."
    return False, (
        f"{label} khởi động nhưng CDP ({endpoint}) không lên sau "
        f"{int(cdp_timeout)}s. Có thể app đang update hoặc crash."
    )


def ensure_cici(log=print, cdp_timeout: float = 30.0) -> tuple[bool, str]:
    """Compat wrapper — ensure_app('cici')."""
    return ensure_app("cici", log=log, cdp_timeout=cdp_timeout)


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


def check_login(provider: str = "cici", timeout: float = 5.0) -> tuple[bool, str]:
    """Qua CDP xem app của provider đã login ByteDance chưa.

    Heuristic: query CDP /json, tìm chat page (theo chat_host pattern của
    provider), kiểm tra title/url không phải guest. Trả (logged_in, detail).
    Best-effort — false negative có thể.
    """
    host = _provider_cfg(provider).get("chat_host", "dola")
    try:
        r = httpx.get(f"{_cdp_endpoint(provider)}/json", timeout=timeout)
        tabs = r.json()
    except Exception as e:
        return False, f"không đọc được CDP tabs: {e}"

    chat_tabs = [t for t in tabs if host in t.get("url", "")]
    if not chat_tabs:
        return False, f"không tìm thấy tab chat của {provider}."
    # App chưa login thường redirect về trang login hoặc title chứa /login
    sample = chat_tabs[0]
    url = sample.get("url", "")
    title = sample.get("title", "")
    if "/login" in url or "login" in title.lower():
        return False, "App đang ở trang login."
    return True, f"App có vẻ đã login (tab: {url})."