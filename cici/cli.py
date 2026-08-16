"""cici CLI — gen ảnh/video qua app Cici (Dola Browser).

Thin client gọi cici-api core server. Cần core server + Cici chạy trước.

Exit codes (cho AI agent phân biệt):
    0 = COMPLETED        1 = FAILED (job)
    2 = TIMEOUT          3 = PREFLIGHT (server/Cici chưa chạy)
    4 = QUOTA_EXHAUSTED (hết quota hằng ngày — đừng retry ngay, chờ reset)
"""
from __future__ import annotations

import json as _json
import logging
import sys
import time

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from . import _client as api
from . import _config
from . import _launcher
from . import _quota

# httpx log mọi request ở INFO — ồn cho CLI; chỉ hiện WARN+
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

console = Console(stderr=True)  # human-facing log -> stderr, stdout giữ JSON/URLs sạch cho agent
out_console = Console()         # stdout (cho URLs / JSON)

# Timeout fallback (giây) — dùng khi server cũ không trả timeout_s trong 202.
# Giá trị thật luôn lấy từ server (config.yaml timing), tránh lệch nhau.
TIMEOUTS = {"image": 300, "video": 600}


def _humanize_eta(seconds: float | int | None) -> str:
    """Format số giây thành 'Xh Ym' / 'Ym Zs' / 'Zs'. None → '?'."""
    if seconds is None or seconds < 0:
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _quota_hint_lines(quota_info: dict | None) -> list[str]:
    """Render hint lines cho panel QUOTA_EXHAUSTED từ dict snapshot (image/video).

    Trả list string (rỗng nếu quota_info không có). Lines:
      - "Còn ~Xh Ym" nếu oldest_unlock_at / reset_in_seconds có
      - "[daily cap · chờ rolling window]" hoặc "[rate-limit burst · thử lại 5-10 phút]"
    """
    if not isinstance(quota_info, dict):
        return []
    lines: list[str] = []
    # ETA tới oldest unlock (= oldest entry rolls out window)
    eta = quota_info.get("reset_in_seconds")
    if isinstance(eta, (int, float)) and eta >= 0:
        lines.append(f"Còn ~{_humanize_eta(eta)} tới khi slot cũ nhất roll ra window.")
    # Phân loại limit-hit
    ltype = quota_info.get("last_limit_type")
    if ltype == "daily":
        lines.append("[yellow]Kiểu daily cap — chờ rolling window (có thể vài giờ).[/]")
    elif ltype == "burst":
        lines.append("[yellow]Kiểu rate-limit burst — thử lại 5-10 phút có thể qua.[/]")
    return lines


def _alt_provider_hint(current: str) -> str | None:
    """Gợi ý đổi provider khi quota provider hiện tại cạn.

    Quota tách theo app (Cici/Doubao không chung window) — provider khác còn
    khả dụng (exe cài sẵn hoặc CDP đang sống) là "quota dự phòng" dùng được
    ngay. Trả None nếu không có lựa chọn thay thế.
    """
    try:
        provs = _launcher._providers_cfg()
    except Exception:  # noqa: BLE001 — config hỏng thì im lặng, đừng phá hint gốc
        return None
    alts = []
    for name, p in provs.items():
        if name == current:
            continue
        if _launcher._find_app_exe(name) or \
                _launcher._cdp_alive(_launcher._cdp_endpoint(name)):
            alts.append((name, p.get("label", name)))
    if not alts:
        return None
    flags = " / ".join(f"--provider {n}" for n, _ in alts)
    labels = " / ".join(lbl for _, lbl in alts)
    return (f"Quota {labels} là RIÊNG (app khác, không chung window) — "
            f"chạy lại với {flags} để gen ngay.")


def _quota_hint_with_alts(quota_info: dict | None, provider: str) -> list[str]:
    """_quota_hint_lines + gợi ý đổi provider (nếu có) — dùng chung cho cả
    Panel lẫn JSON hint array."""
    lines = _quota_hint_lines(quota_info)
    alt = _alt_provider_hint(provider)
    if alt:
        lines.append(alt)
    return lines


def _quota_sleep(delay: float, reason: str, kind: str, attempt: int) -> None:
    """Ngủ `delay` giây theo chunk 60s — Ctrl+C phản hồi nhanh + tiến độ định kỳ.

    Progress chỉ ra stderr (console) — stdout giữ sạch cho JSON cuối."""
    console.print(
        f"[yellow]↻ Quota {kind} bị chặn ({reason}) — chờ {_humanize_eta(delay)} "
        f"rồi tự re-enqueue (lần {attempt + 1})…[/]"
    )
    remaining = delay
    while remaining > 0:
        step = min(60.0, remaining)
        time.sleep(step)
        remaining -= step
        if remaining > 0:
            console.print(f"[dim]  còn {_humanize_eta(remaining)}…[/]")


def _quota_wait_then_retry(quota_info: dict | None, kind: str, attempt: int,
                           deadline: float, qcfg: dict,
                           unknown_retry: float) -> bool:
    """Tính + ngủ thời gian chờ quota. Trả True nếu nên re-enqueue tiếp,
    False nếu vượt --quota-max-wait (caller exit 4 như cũ)."""
    delay, reason = _quota.plan_retry(
        quota_info, kind,
        burst_retry_seconds=float(qcfg.get("burst_retry_seconds", 300.0)),
        unknown_retry_seconds=unknown_retry,
        resume_buffer_seconds=float(qcfg.get("resume_buffer_seconds", 15.0)),
    )
    if delay is None:
        delay, reason = unknown_retry, "thiếu dữ kiện ETA"
    if time.time() + delay > deadline:
        console.print(
            f"[yellow]Quota {kind} vẫn cạn — cần chờ {_humanize_eta(delay)} nữa nhưng vượt "
            f"--quota-max-wait (còn {_humanize_eta(max(0.0, deadline - time.time()))}) → dừng.[/]"
        )
        return False
    _quota_sleep(delay, reason, kind, attempt)
    return True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _preflight(base: str, auto_launch: bool = True, provider: str = "cici") -> bool:
    """Đảm bảo core server + app của provider sẵn sàng. Trả True nếu OK.

    Với auto_launch=True: nếu thiếu server/app → tự khởi động ngầm, rồi retry.
    Với auto_launch=False: chỉ check, báo hướng dẫn nếu thiếu.
    """
    if auto_launch:
        return _preflight_auto(base, provider)
    return _preflight_manual(base)


def _preflight_auto(base: str, provider: str = "cici") -> bool:
    """Auto-launch app (Cici/Doubao) + spawn server nếu thiếu, rồi verify."""
    label = {"cici": "Cici", "doubao": "Doubao"}.get(provider, provider)
    # 1. App có CDP
    if not _launcher._cdp_alive(_launcher._cdp_endpoint(provider)):
        ok, msg = _launcher.ensure_app(provider, log=console.print)
        if not ok:
            out_console.print(Panel.fit(f"[red]{msg}[/]", title="[red]Preflight failed", border_style="red"))
            return False
        console.print(f"[green]✓ {msg}[/]")
    # 2. Login check
    logged_in, detail = _launcher.check_login(provider)
    if not logged_in:
        out_console.print(
            Panel.fit(
                f"[bold red]{label} chưa đăng nhập[/]\n{detail}\n\n"
                "[bold]Cách khắc phục:[/]\n"
                f"  Mở cửa sổ {label}, đăng nhập account ByteDance của bạn,\n"
                "  rồi chạy lại lệnh này. Tool không thể tự login hộ bạn.",
                title=f"[red]Cần đăng nhập {label}",
                border_style="red",
            )
        )
        return False
    # 3. Core server
    if not _launcher._api_alive():
        ok, msg = _launcher.ensure_server(log=console.print)
        if not ok:
            out_console.print(Panel.fit(f"[red]{msg}[/]", title="[red]Preflight failed", border_style="red"))
            return False
        console.print(f"[green]✓ {msg}[/]")
    # 4. Final health verify qua core (worker connect Cici OK?)
    try:
        h = api.health(base=base)
    except api.CiciUnreachable as e:
        out_console.print(f"[red]✗ Server lên nhưng health lỗi: {e}[/]")
        return False
    if h.get("status") != "ok":
        out_console.print(f"[red]✗ Cici CDP không nối được: {h}[/]")
        return False
    # 5. Peek quota — nếu đã cạn, in hint với ETA để user biết trước khi enqueue
    # (vẫn cho phép tiếp tục — không block; chỉ thông báo).
    try:
        snap = api.quota(base=base, timeout=2.0, provider=provider)
        for kind, info in snap.items():
            rmn = info.get("remaining")
            if rmn is not None and rmn == 0:
                hints = _quota_hint_with_alts(info, provider)
                hint_block = "\n".join(hints)
                tail = f"\n{hint_block}" if hint_block else ""
                console.print(
                    f"[yellow]⚠ Quota {kind} local estimate = 0 — job sẽ bị 429.[/]"
                    f"{tail}"
                )
                break
    except Exception:  # noqa: BLE001 — quota peek là best-effort, không bao giờ chặn preflight
        pass
    return True


def _preflight_manual(base: str) -> bool:
    """Chỉ check, in hướng dẫn nếu thiếu (không auto-launch)."""
    try:
        h = api.health(base=base)
    except api.CiciUnreachable as e:
        out_console.print(
            Panel.fit(
                f"[bold red]Core server không trả lời[/]\n"
                f"endpoint: {base}\nlỗi: {e}\n\n"
                "[bold]Cách khắc phục:[/]\n"
                "  1. Khởi động Cici có CDP: start_cici.bat\n"
                "  2. Khởi động server: python -m cici.server\n"
                "  3. Chạy lại. (Hoặc bỏ --no-auto-launch để CLI tự khởi động.)",
                title="[red]Preflight failed", border_style="red",
            )
        )
        return False
    if h.get("status") != "ok":
        out_console.print(
            Panel.fit(
                f"[bold red]Cici CDP không nối được[/]\nhealth: {h}\n\n"
                "Cici phải chạy với --remote-debugging-port=9222. Chạy start_cici.bat.",
                title="[red]Preflight failed", border_style="red",
            )
        )
        return False
    return True


def _emit_json(obj: dict) -> None:
    """In JSON sạch ra stdout (agent parse được)."""
    out_console.print_json(_json.dumps(obj, ensure_ascii=False, default=str))


def _render_result(job: dict, elapsed: float) -> None:
    """In kết quả COMPLETED dạng gọn: 1 dòng/URL, không table (tiết kiệm token)."""
    urls = job.get("result_urls") or []
    kind = job.get("kind", "?")
    out_console.print(f"[bold green]✓ COMPLETED[/] {kind} {elapsed:.1f}s {len(urls)} kết quả")
    if not urls:
        out_console.print("[yellow](không có URL kết quả)[/]")
        return
    for u in urls:
        # soft_wrap: URL dài KHÔNG được gãy dòng — khách copy/download nguyên vẹn
        out_console.print(u, soft_wrap=True)
    # cảnh báo expiry ngắn (chỉ khi cần — đừng tốn token cho URL còn hạn dài)
    soon = [u for u in urls if (api.seconds_until_expiry(u) or 1e9) < 3600]
    if soon:
        out_console.print(
            "[bold red]⚠ URL sắp hết hạn (<1h) — download ngay nếu cần.[/]"
        )


def _run_generation(prompt: str, kind: str, as_json: bool, base: str,
                    model: str | None = None, auto_launch: bool = True,
                    references: list[str] | None = None,
                    ratio: str | None = None, style: str | None = None,
                    duration: str | None = None,
                    quota_wait: bool = False, quota_max_wait: float | None = None,
                    account: str | None = None,
                    provider: str = "cici") -> int:
    """Luồng chung cho image/video: preflight -> generate -> wait -> render.

    provider = "cici" (mặc định) hoặc "doubao" — app/CDP endpoint + registry
    + quota state riêng của provider đó.

    quota_wait=True: khi bị quota chặn (429 lúc enqueue hoặc QUOTA_EXHAUSTED
    giữa job), thay vì exit 4 ngay thì chờ tới khi slot cũ nhất roll ra rolling
    window (daily cap) hoặc vài phút (burst), rồi tự re-enqueue. Bounded bởi
    --quota-max-wait + quota.max_attempts — vượt giới hạn thì exit 4 như cũ.
    """
    if not _preflight(base, auto_launch=auto_launch, provider=provider):
        return api.EXIT_PREFLIGHT

    if quota_wait:
        try:
            qcfg = _config.load_config().get("quota") or {}
        except Exception:  # noqa: BLE001 — config hỏng thì dùng default, không chặn gen
            qcfg = {}
    else:
        qcfg = {}
    max_attempts = max(1, int(qcfg.get("max_attempts", 3))) if quota_wait else 1
    if quota_max_wait is None:
        quota_max_wait = float(qcfg.get("max_wait_seconds", 21600.0))
    deadline = time.time() + quota_max_wait
    unknown_retry = float(qcfg.get("unknown_retry_seconds", 120.0))

    timeout = TIMEOUTS[kind]
    t0 = time.time()

    attempt = 0
    while True:
        attempt += 1
        if attempt > 1:
            console.print(f"[yellow]↻ retry {attempt}/{max_attempts}…[/]")

        try:
            resp = api.generate(prompt, kind, base=base, model=model, references=references,
                                ratio=ratio, style=style, duration=duration,
                                account=account, provider=provider)
        except api.QuotaExhausted as e:
            # server refuse vì local quota estimate = 0 (đừng lãng phí thời gian gen)
            quota_snap = e.detail.get("quota") if isinstance(e.detail, dict) else None
            hint_lines = _quota_hint_with_alts(
                quota_snap if isinstance(quota_snap, dict) else None, provider)
            if quota_wait and attempt < max_attempts \
                    and _quota_wait_then_retry(quota_snap, kind, attempt, deadline,
                                               qcfg, unknown_retry):
                continue
            if as_json:
                _emit_json({
                    "status": "QUOTA_EXHAUSTED",
                    "kind": kind,
                    "detail": e.detail,
                    "hint": hint_lines,
                })
            else:
                hint_block = "\n".join(hint_lines)
                out_console.print(
                    Panel.fit(
                        f"[bold red]Quota {kind} đã cạn (local estimate)[/]\n"
                        f"{e.detail.get('message', '') if isinstance(e.detail, dict) else ''}\n\n"
                        f"{hint_block}\n\n"
                        "[dim]Chạy `cici quota` xem chi tiết. Agent: đọc "
                        "detail.quota.suggested_retry_after để schedule lại. "
                        "Hoặc chạy lại với --wait-for-quota để CLI tự chờ + retry.[/]",
                        title="[red]Quota exhausted", border_style="red",
                    )
                )
            return api.EXIT_QUOTA
        except ValueError as e:  # invalid alias/option/prompt (server 422)
            # hint alias chỉ đúng cho lỗi unknown model/ratio/style/duration —
            # với lỗi prompt thì hint này gây hiểu lầm
            hint = ("Chạy `cici models` để xem alias hợp lệ."
                    if str(e).startswith("Unknown ") else None)
            if as_json:
                payload = {"status": "FAILED", "error": str(e)}
                if hint:
                    payload["hint"] = hint
                _emit_json(payload)
            else:
                line = f"[red]✗ {e}[/]"
                if hint:
                    line += f"\n[dim]{hint}[/]"
                out_console.print(line)
            return api.EXIT_FAILED
        except Exception as e:  # noqa: BLE001 — server chết / mất kết nối lúc enqueue
            if as_json:
                _emit_json({"status": "ENQUEUE_ERROR", "error": str(e),
                            "hint": "core server không trả lời khi enqueue — thử lại khi server lên lại"})
            else:
                out_console.print(f"[red]Lỗi khi enqueue job: {e}[/]")
            return api.EXIT_PREFLIGHT

        code, job = _wait_job_and_handle(resp, kind, as_json, base, model, timeout, t0)
        if code is not None:
            return code

        # QUOTA_EXHAUSTED giữa job — job mang theo snapshot quota để tính thời điểm chờ
        quota_snap = job.get("quota")
        hint_lines = _quota_hint_with_alts(
            quota_snap if isinstance(quota_snap, dict) else None, provider)
        if quota_wait and attempt < max_attempts \
                and _quota_wait_then_retry(quota_snap, kind, attempt, deadline,
                                           qcfg, unknown_retry):
            continue
        if as_json:
            _emit_json({
                "status": "QUOTA_EXHAUSTED",
                "job_id": resp["job_id"],
                "kind": kind,
                "message": job.get("message"),
                "quota": quota_snap,
                "hint": hint_lines,
            })
        else:
            hint_block = "\n".join(hint_lines)
            tail = f"\n{hint_block}" if hint_block else ""
            out_console.print(
                Panel.fit(
                    f"[bold red]Cici báo hết quota {kind}[/]\n"
                    f"[dim]{job.get('message', '')}[/]\n"
                    f"{tail}\n\n"
                    "[dim]Đừng retry ngay — chờ rolling window. Agent: "
                    "đọc quota.suggested_retry_after. "
                    "Hoặc chạy lại với --wait-for-quota để CLI tự chờ + retry.[/]",
                    title="[red]Quota exhausted", border_style="red",
                )
            )
        return api.EXIT_QUOTA


def _wait_job_and_handle(resp: dict, kind: str, as_json: bool, base: str,
                         model: str | None, timeout: float,
                         t0: float) -> tuple[int | None, dict]:
    """Chờ job tới trạng thái terminal + render kết quả (COMPLETED/FAILED/…).

    Trả (exit_code, job_dict):
      - QUOTA_EXHAUSTED → (None, job) — caller quyết retry (--wait-for-quota)
        hay emit + exit 4. Không emit gì ở đây để stdout JSON giữ 1 emit duy nhất.
      - Các trạng thái khác → (exit_code, {}) sau khi đã emit kết quả cuối.
    """
    job_id = resp["job_id"]
    # server trả timeout thật (theo config.yaml timing) — dùng thay vì fallback
    timeout = float(resp.get("timeout_s") or timeout)

    if as_json:
        # JSON mode: in progress tối thiểu ra stderr, kết quả JSON ra stdout ở cuối
        console.print(f"[dim]job {job_id} enqueued ({kind}/{model or 'default'}), chờ tới {timeout}s…[/]")
    else:
        out_console.print(
            f"[dim]job {job_id} · đang gen {kind}" +
            (f"/{model}" if model else "") +
            f"… (timeout {timeout}s)[/]"
        )

    last_status = "PENDING"

    def on_tick(s: dict) -> None:
        nonlocal last_status
        st = s.get("status", "?")
        if st != last_status:
            console.print(f"[dim]  {last_status} → {st}[/]")
            last_status = st

    try:
        job = api.wait_status(job_id, timeout=timeout, base=base, on_tick=on_tick)
    except TimeoutError as e:
        if as_json:
            _emit_json({"status": "TIMEOUT", "job_id": job_id, "error": str(e)})
        else:
            out_console.print(f"[bold red]✗ TIMEOUT: {e}[/]")
        return api.EXIT_TIMEOUT, {}
    except Exception as e:  # noqa: BLE001 — mất kết nối server giữa chừng poll
        # Job có thể vẫn đang chạy server-side — agent nên poll lại bằng `status`.
        if as_json:
            _emit_json({
                "status": "POLL_ERROR",
                "job_id": job_id,
                "error": str(e),
                "hint": f"server mất kết nối khi đang poll — chạy `cici status {job_id}` khi server trở lại",
            })
        else:
            out_console.print(
                f"[bold red]✗ Mất kết nối server khi đang poll job {job_id}[/]\n"
                f"[dim]{e}[/]\n"
                f"Job có thể vẫn đang chạy. Khi server trở lại: [bold]cici status {job_id}[/]"
            )
        return api.EXIT_PREFLIGHT, {}

    elapsed = time.time() - t0
    status = job.get("status")

    if status == "QUOTA_EXHAUSTED":
        # Cici thực sự báo hết quota trong lúc gen — snapshot đi kèm để caller
        # tính thời điểm chờ (daily/burst) nếu chạy với --wait-for-quota.
        return None, job

    if status == "CONTENT_BLOCKED":
        # Cici ĐÃ gen xong nhưng từ chối hiển thị kết quả (bản quyền / content policy).
        # Filter của Cici — không phải lỗi tool. Đổi nội dung tham chiếu / prompt rồi retry.
        if as_json:
            _emit_json({
                "status": "CONTENT_BLOCKED",
                "job_id": job_id,
                "kind": kind,
                "message": job.get("message"),
            })
        else:
            out_console.print(
                Panel.fit(
                    f"[bold red]Cici từ chối kết quả (bản quyền / content policy)[/]\n"
                    f"[dim]{(job.get('message') or '')[:240]}[/]\n\n"
                    "[bold]Cách khắc phục:[/]\n"
                    "  • Đổi ảnh tham chiếu khác, hoặc sửa prompt.\n"
                    "  • Với video dính \"âm thanh\": thử thêm 'no sound' / 'silent' / 'ambient only'.\n"
                    "  Đây là filter của Cici, không phải lỗi tool.",
                    title="[red]Content blocked", border_style="red",
                )
            )
        return api.EXIT_FAILED, {}

    if status == "COMPLETED":
        if as_json:
            payload = {
                "status": "COMPLETED",
                "job_id": job_id,
                "kind": kind,
                "elapsed_s": round(elapsed, 1),
                "urls": [
                    {
                        "url": u,
                        "expires_unix": api.parse_expiry(u),
                        "expires_local": (
                            api.expiry_local(u).isoformat() if api.expiry_local(u) else None
                        ),
                    }
                    for u in (job.get("result_urls") or [])
                ],
            }
            _emit_json(payload)
        else:
            _render_result(job, elapsed)
        return api.EXIT_OK, {}

    # FAILED
    if as_json:
        _emit_json({"status": "FAILED", "job_id": job_id, "error": job.get("error")})
    else:
        out_console.print(
            f"[bold red]✗ FAILED[/] · {elapsed:.1f}s\n[dim]{job.get('error')}[/]"
        )
    return api.EXIT_FAILED, {}


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="cici")
@click.option("--base", default=api.DEFAULT_BASE, show_default=True,
              help="cici-api core URL (hoặc set env CICI_API).")
@click.option("--no-auto-launch", is_flag=True, default=False,
              help="Không tự khởi động Cici/server khi thiếu (chỉ check + hướng dẫn).")
@click.pass_context
def main(ctx: click.Context, base: str, no_auto_launch: bool):
    """Gen ảnh/video qua app Cici (Dola Browser).

    Mặc định CLI TỰ khởi động Cici + core server nếu chưa chạy.
    Dùng --no-auto-launch để tắt (chỉ check, báo hướng dẫn).
    """
    ctx.ensure_object(dict)
    ctx.obj["base"] = base
    ctx.obj["auto_launch"] = not no_auto_launch
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.pass_context
def health(ctx: click.Context, as_json: bool):
    """Check core server + Cici CDP reachable (không auto-launch)."""
    base = ctx.obj["base"]
    # health thuần check, không auto (để agent biết trạng thái thật)
    cdp_up = _launcher._cdp_alive()
    api_up = _launcher._api_alive()
    if api_up:
        try:
            h = api.health(base=base)
        except api.CiciUnreachable:
            h = {"status": "error"}
    else:
        h = {"status": "unreachable"}
    if as_json:
        _emit_json({**h, "cici_cdp_up": cdp_up, "auto_launch_available": ctx.obj["auto_launch"]})
    else:
        cdp_icon = "[green]✓[/]" if cdp_up else "[red]✗[/]"
        api_icon = "[green]✓[/]" if api_up else "[red]✗[/]"
        out_console.print(
            f"{api_icon} core server: {h.get('status')} ({base})\n"
            f"{cdp_icon} Cici CDP: {'up' if cdp_up else 'down (sẽ auto-launch khi gen)'}\n"
            f"{'  → sẽ tự khởi động Cici+server nếu thiếu khi chạy image/video' if ctx.obj['auto_launch'] else '  → auto-launch TẮT (--no-auto-launch)'}"
        )
    sys.exit(api.EXIT_OK if (h.get("status") == "ok" and cdp_up) else api.EXIT_PREFLIGHT)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.pass_context
def doctor(ctx: click.Context, as_json: bool):
    """Check prerequisites: app Cici, đăng nhập, CDP, server, config.

    Read-only — không tự khởi động/sửa gì. Exit 0 khi sẵn sàng gen,
    1 khi có mục FAIL (xem hướng dẫn fix trong output).
    """
    import sys as _sys

    from . import _config, _launcher

    checks: list[dict] = []

    def add(name: str, status: str, detail: str = "", fix: str = "") -> None:
        checks.append({"name": name, "status": status, "detail": detail, "fix": fix})

    # 1. Python
    v = _sys.version_info
    add("python", "ok" if v >= (3, 10) else "fail",
        f"{_sys.version.split()[0]} ({_sys.executable})",
        "" if v >= (3, 10) else "Cài Python >= 3.10.")

    # 2. Package/server importable (deps đầy đủ?)
    try:
        import cici.server  # noqa: F401
        add("cici-server", "ok", "import cici.server OK (fastapi/uvicorn/playwright có sẵn)")
    except Exception as e:  # noqa: BLE001
        add("cici-server", "fail", f"import lỗi: {e}",
            "Cài lại đầy đủ: pip install -r requirements.txt (hoặc pipx reinstall).")

    # 3. Config resolvable
    try:
        cfg_path = _config.config_path()
        _config.load_config(cfg_path)
        if cfg_path == _config.USER_CONFIG:
            where = "user (~/.cici)"
        elif cfg_path == _config.PACKAGED_CONFIG:
            where = "packaged"
        else:
            where = "repo/cwd"
        add("config", "ok", f"{cfg_path} ({where})")
    except Exception as e:  # noqa: BLE001
        add("config", "fail", f"không đọc được config: {e}", "Xem ~/.cici/config.yaml hoặc set CICI_CONFIG.")

    # 4. Cici app installed
    exe = _launcher._find_cici_exe()
    if _sys.platform == "win32":
        add("cici-app", "ok" if exe else "fail",
            exe or "không tìm thấy Cici.exe",
            "" if exe else "Cài Cici/Dola Browser, hoặc set env CICI_EXE=<đường dẫn Cici.exe>.")
    else:
        add("cici-app", "warn" if not exe else "ok",
            exe or "không kiểm tra được trên nền tảng này (cần CDP thủ công)",
            "Chạy Cici với --remote-debugging-port=9222 trước khi gen.")

    # 5. CDP
    cdp_up = _launcher._cdp_alive()
    add("cici-cdp", "ok" if cdp_up else "warn",
        "http://127.0.0.1:9222 up" if cdp_up else "CDP chưa lên (sẽ tự khởi động Cici khi gen)",
        "" if cdp_up else "Mở Cici có CDP: start_cici.bat — hoặc để CLI tự launch.")

    # 6. Login (chỉ check khi CDP up)
    if cdp_up:
        logged_in, detail = _launcher.check_login()
        add("cici-login", "ok" if logged_in else "fail", detail,
            "" if logged_in else "Mở cửa sổ Cici, đăng nhập tài khoản ByteDance, rồi chạy lại doctor.")
    else:
        add("cici-login", "warn", "chưa check được (CDP xuống)",
            "Chạy lại doctor sau khi Cici/CDP lên.")

    # 7. Core server
    api_up = _launcher._api_alive()
    add("core-server", "ok" if api_up else "warn",
        "http://127.0.0.1:8000 up" if api_up else "chưa chạy (sẽ tự spawn khi gen)",
        "" if api_up else "Không cần làm gì — CLI tự spawn. Muốn chạy tay: python -m cici.server")

    # 8. Quota file
    try:
        from . import _quota
        _quota.load()
        add("quota-state", "ok", f"{_quota.DEFAULT_STATE_PATH}")
    except Exception as e:  # noqa: BLE001
        add("quota-state", "warn", f"đọc lỗi: {e} (quota tracking sẽ fail-open)")

    ready = all(c["status"] != "fail" for c in checks)
    if as_json:
        _emit_json({"ready": ready, "checks": checks})
    else:
        icon = {"ok": "[green]✓[/]", "warn": "[yellow]![/]", "fail": "[red]✗[/]"}
        tbl = Table(title="cici doctor", show_lines=False)
        tbl.add_column("", width=3)
        tbl.add_column("check", style="cyan")
        tbl.add_column("detail", overflow="fold")
        for c in checks:
            tbl.add_row(icon[c["status"]], c["name"], c["detail"])
        out_console.print(tbl)
        fails = [c for c in checks if c["status"] == "fail"]
        if fails:
            out_console.print("\n[bold red]Cần khắc phục:[/]")
            for c in fails:
                out_console.print(f"  • {c['name']}: {c['fix'] or c['detail']}", soft_wrap=True)
        else:
            out_console.print("\n[bold green]✓ Sẵn sàng gen[/] (các mục [yellow]![/] là warn, không chặn)")
    sys.exit(api.EXIT_OK if ready else api.EXIT_FAILED)


@main.command()
@click.argument("prompt")
@click.option("-m", "--model", default=None, help="Model alias (xem `cici models`).")
@click.option("--ref", "refs", multiple=True,
              help="Ảnh tham chiếu (đường dẫn local). Lặp lại được, hoặc dùng dấu phẩy: --ref a.png,b.png. Tối đa 10.")
@click.option("--ratio", default=None,
              help="Tỷ lệ khung hình (xem `cici models`): 1:1, 2:3, 3:4, 4:3, 9:16, 16:9.")
@click.option("--style", default=None,
              help="Phong cách (xem `cici models`): portrait, landscape, anime, 3d, cyberpunk, oil-painting, watercolor, ...")
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.option("--wait-for-quota", "quota_wait", is_flag=True, default=False,
              help="Hết quota thì tự chờ slot roll ra rolling 24h rồi re-enqueue (giới hạn bởi --quota-max-wait).")
@click.option("--quota-max-wait", "quota_max_wait", type=float, default=None,
              help="Tổng giây chờ quota tối đa khi --wait-for-quota (mặc định từ config quota.max_wait_seconds).")
@click.option("--account", "account", default=None,
              help="Nhãn tách quota local theo account (bạn TỰ đổi account trong app Cici — tool không tự đổi).")
@click.option("--provider", "provider",
              type=click.Choice(["cici", "doubao"]), default="cici",
              help="App để gen: cici (Dola, mặc định) hoặc doubao (豆包 — bản TQ, quota riêng).")
@click.pass_context
def image(ctx: click.Context, prompt: str, model: str | None, refs: tuple[str, ...],
          ratio: str | None, style: str | None, as_json: bool,
          quota_wait: bool, quota_max_wait: float | None,
          account: str | None, provider: str):
    """Sinh ảnh từ PROMPT (block tới xong, ~2-3 phút).

    Model mặc định: seedream-5-pro. Đổi bằng -m/--model (xem `cici models`).
    Thêm ảnh tham chiếu bằng --ref (nhiều lần hoặc phân tách bằng dấu phẩy).
    Chọn tỷ lệ khung hình --ratio và phong cách --style.
    """
    # flatten: mỗi --ref có thể chứa nhiều path comma-separated
    ref_list: list[str] = []
    for r in refs:
        for part in r.split(","):
            part = part.strip()
            if part:
                ref_list.append(part)
    sys.exit(_run_generation(prompt, "image", as_json, ctx.obj["base"], model=model,
                             auto_launch=ctx.obj["auto_launch"],
                             references=ref_list or None,
                             ratio=ratio, style=style,
                             quota_wait=quota_wait, quota_max_wait=quota_max_wait,
                             account=account, provider=provider))


@main.command()
@click.argument("prompt")
@click.option("-m", "--model", default=None, help="Model alias (xem `cici models`).")
@click.option("--ref", "refs", multiple=True,
              help="Ảnh tham chiếu / frame đầu (image-to-video, đường dẫn local). Lặp lại được, hoặc dấu phẩy: --ref a.png,b.png. Tối đa 10.")
@click.option("--ratio", default=None,
              help="Tỷ lệ khung hình (xem `cici models`): 1:1, 3:4, 4:3, 9:16, 16:9, 21:9.")
@click.option("--duration", default=None, help="Thời lượng: 5s hoặc 10s.")
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.option("--wait-for-quota", "quota_wait", is_flag=True, default=False,
              help="Hết quota thì tự chờ slot roll ra rolling 24h rồi re-enqueue (giới hạn bởi --quota-max-wait).")
@click.option("--quota-max-wait", "quota_max_wait", type=float, default=None,
              help="Tổng giây chờ quota tối đa khi --wait-for-quota (mặc định từ config quota.max_wait_seconds).")
@click.option("--account", "account", default=None,
              help="Nhãn tách quota local theo account (bạn TỰ đổi account trong app Cici — tool không tự đổi).")
@click.option("--provider", "provider",
              type=click.Choice(["cici", "doubao"]), default="cici",
              help="App để gen: cici (Dola, mặc định) hoặc doubao (豆包 — bản TQ, quota riêng).")
@click.pass_context
def video(ctx: click.Context, prompt: str, model: str | None, refs: tuple[str, ...],
          ratio: str | None, duration: str | None, as_json: bool,
          quota_wait: bool, quota_max_wait: float | None,
          account: str | None, provider: str):
    """Sinh video từ PROMPT (block tới xong).

    Model mặc định: seedance-2.5. Hỗ trợ image-to-video: truyền ảnh bằng --ref.
    Chọn tỷ lệ khung hình --ratio và thời lượng --duration (5s/10s).
    """
    ref_list: list[str] = []
    for r in refs:
        for part in r.split(","):
            part = part.strip()
            if part:
                ref_list.append(part)
    sys.exit(_run_generation(prompt, "video", as_json, ctx.obj["base"], model=model,
                             auto_launch=ctx.obj["auto_launch"],
                             references=ref_list or None,
                             ratio=ratio, duration=duration,
                             quota_wait=quota_wait, quota_max_wait=quota_max_wait,
                             account=account, provider=provider))


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.option("--account", "account", default=None,
              help="Xem quota riêng của 1 nhãn account (state ~/.cici/quota-<nhãn>.json).")
@click.option("--provider", "provider",
              type=click.Choice(["cici", "doubao"]), default="cici",
              help="Xem quota của provider (mặc định cici).")
@click.pass_context
def quota(ctx: click.Context, as_json: bool, account: str | None, provider: str):
    """Xem quota còn lại (rolling 24h local estimate + threshold đã học).

    --account <nhãn> để xem quota riêng của nhãn đó. Bạn tự đổi account trong
    app Cici và gán nhãn khi gen — tool KHÔNG tự đổi account.
    """
    base = ctx.obj["base"]
    try:
        snap = api.quota(base=base, account=account, provider=provider)
    except api.CiciUnreachable as e:
        if as_json:
            _emit_json({"status": "unreachable", "error": str(e)})
        else:
            out_console.print(f"[red]✗ Core server không trả lời: {e}[/]")
        sys.exit(api.EXIT_PREFLIGHT)
    except Exception as e:  # 501 nếu quota tracking unavailable
        if as_json:
            _emit_json({"status": "unavailable", "error": str(e)})
        else:
            out_console.print(f"[yellow]⚠ Quota tracking chưa sẵn sàng: {e}[/]")
        sys.exit(api.EXIT_FAILED)

    if as_json:
        _emit_json({"account": account, **snap})
        sys.exit(api.EXIT_OK)

    # human render
    for kind, info in snap.items():
        used = info.get("used_in_window", 0)
        thr = info.get("threshold")
        rmn = info.get("remaining")
        window = info.get("window_hours", 24)
        reset = info.get("reset_in_seconds")
        ltype = info.get("last_limit_type")
        title = f"Quota · {kind} (rolling {window}h)"
        if account:
            title += f" · account {account}"
        tbl = Table(title=title, show_lines=False)
        tbl.add_column("metric", style="cyan")
        tbl.add_column("value")
        tbl.add_row("used", str(used))
        tbl.add_row("threshold (auto-learn)", str(thr) if thr is not None else "[dim]chưa học (chưa từng hit limit)[/]")
        if rmn is not None:
            color = "green" if rmn > 0 else "red"
            tbl.add_row("remaining", f"[{color}]{rmn}[/{color}]")
        if reset is not None:
            tbl.add_row("reset trong", f"{_humanize_eta(reset)} (khi gen cũ nhất roll ra khỏi window)")
        if ltype:
            label = {
                "daily": "[yellow]daily cap[/] — chờ rolling window",
                "burst": "[yellow]rate-limit burst[/] — có thể qua sau vài phút",
            }.get(ltype, ltype)
            tbl.add_row("limit type cuối", label)
        out_console.print(tbl)
        out_console.print()
    sys.exit(api.EXIT_OK)


@main.command()
@click.option("--type", "kind", type=click.Choice(["image", "video"]),
              default=None, help="Lọc theo loại (image/video).")
@click.option("--provider", "provider",
              type=click.Choice(["cici", "doubao"]), default="cici",
              help="Registry của provider (mặc định cici; doubao = model TQ).")
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.pass_context
def models(ctx: click.Context, kind: str | None, provider: str, as_json: bool):
    """List các model khả dụng + generation options (ratio/style/duration)."""
    base = ctx.obj["base"]
    try:
        registry = api.models(base=base, provider=provider)
    except api.CiciUnreachable as e:
        # fallback: nếu server down, vẫn không list được (registry nằm trong core config)
        if as_json:
            _emit_json({"status": "unreachable", "error": str(e)})
        else:
            out_console.print(
                f"[red]✗ Core server không trả lời: {e}[/]\n"
                "[dim]`cici models` cần core server chạy (python -m cici.server).[/]"
            )
        sys.exit(api.EXIT_PREFLIGHT)

    # /api/models trả {models: {...}, options: {...}}; chấp nhận shape cũ (chỉ models)
    mods = registry.get("models", registry)
    opts = registry.get("options", {})
    if kind:
        mods = {kind: mods.get(kind, {})}
        opts = {kind: opts.get(kind, {})}
    if as_json:
        _emit_json({"models": mods, "options": opts})
        sys.exit(api.EXIT_OK)

    # human table: models
    for modality, info in mods.items():
        if not info:
            continue
        default = info.get("default")
        tbl = Table(title=f"Models · {modality}", show_lines=False)
        tbl.add_column("alias", style="cyan", overflow="fold")
        tbl.add_column("name", overflow="fold")
        tbl.add_column("default", width=8)
        tbl.add_column("note", overflow="fold")
        for opt in info.get("options", []):
            is_default = opt["alias"] == default
            tbl.add_row(
                opt["alias"],
                opt["name"],
                "[green]✓ default[/]" if is_default else "",
                opt.get("note", ""),
            )
        out_console.print(tbl)
        out_console.print()

    # human table: generation options
    for modality, groups in opts.items():
        if not groups:
            continue
        tbl = Table(title=f"Generation options · {modality}", show_lines=False)
        tbl.add_column("group", style="cyan")
        tbl.add_column("aliases", overflow="fold")
        for group, options in groups.items():
            aliases = ", ".join(o["alias"] for o in options)
            tbl.add_row(group, aliases)
        out_console.print(tbl)
        out_console.print()
    sys.exit(api.EXIT_OK)


@main.command()
@click.argument("job_id")
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.pass_context
def status(ctx: click.Context, job_id: str, as_json: bool):
    """Xem trạng thái 1 job (poll một lần, không block)."""
    base = ctx.obj["base"]
    try:
        s = api.status(job_id, base=base)
    except KeyError:
        if as_json:
            _emit_json({"status": "NOT_FOUND", "job_id": job_id})
        else:
            out_console.print(f"[red]✗ Job {job_id} không tồn tại[/]")
        sys.exit(api.EXIT_FAILED)
    except Exception as e:  # noqa: BLE001
        if as_json:
            _emit_json({"status": "ERROR", "error": str(e)})
        else:
            out_console.print(f"[red]✗ {e}[/]")
        sys.exit(api.EXIT_PREFLIGHT)

    st = s.get("status")
    if as_json:
        _emit_json(s)
    else:
        color = {"COMPLETED": "green", "FAILED": "red", "PROCESSING": "yellow"}.get(st, "cyan")
        out_console.print(f"[{color}]{st}[/] · job {job_id}")
        if s.get("result_urls"):
            for u in s["result_urls"]:
                out_console.print(f"  {u}", soft_wrap=True)
        if s.get("error"):
            out_console.print(f"[dim]  err: {s['error']}[/]")
    sys.exit(api.EXIT_OK if st == "COMPLETED" else (api.EXIT_FAILED if st == "FAILED" else api.EXIT_OK))


if __name__ == "__main__":
    main()
