"""cici CLI — gen ảnh/video qua app Cici (Dola Browser).

Thin client gọi cici-api core server. Cần core server + Cici chạy trước.

Exit codes (cho AI agent phân biệt):
    0 = COMPLETED        1 = FAILED (job)
    2 = TIMEOUT          3 = PREFLIGHT (server/Cici chưa chạy)
    4 = QUOTA_EXHAUSTED (hết quota hằng ngày — đừng retry ngay, chờ reset)
"""
from __future__ import annotations

import json as _json
import sys
import time

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from . import _client as api
from . import _launcher

console = Console(stderr=True)  # human-facing log -> stderr, stdout giữ JSON/URLs sạch cho agent
out_console = Console()         # stdout (cho URLs / JSON)

# Timeout fallback (giây) — dùng khi server cũ không trả timeout_s trong 202.
# Giá trị thật luôn lấy từ server (config.yaml timing), tránh lệch nhau.
TIMEOUTS = {"image": 300, "video": 600}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _preflight(base: str, auto_launch: bool = True) -> bool:
    """Đảm bảo core server + Cici sẵn sàng. Trả True nếu OK.

    Với auto_launch=True: nếu thiếu server/Cici → tự khởi động ngầm, rồi retry.
    Với auto_launch=False: chỉ check, báo hướng dẫn nếu thiếu.
    """
    if auto_launch:
        return _preflight_auto(base)
    return _preflight_manual(base)


def _preflight_auto(base: str) -> bool:
    """Auto-launch Cici + spawn server nếu thiếu, rồi verify."""
    # 1. Cici có CDP
    if not _launcher._cdp_alive():
        ok, msg = _launcher.ensure_cici(log=console.print)
        if not ok:
            out_console.print(Panel.fit(f"[red]{msg}[/]", title="[red]Preflight failed", border_style="red"))
            return False
        console.print(f"[green]✓ {msg}[/]")
    # 2. Login check
    logged_in, detail = _launcher.check_login()
    if not logged_in:
        out_console.print(
            Panel.fit(
                f"[bold red]Cici chưa đăng nhập[/]\n{detail}\n\n"
                "[bold]Cách khắc phục:[/]\n"
                "  Mở cửa sổ Cici, đăng nhập account ByteDance của bạn,\n"
                "  rồi chạy lại lệnh này. Tool không thể tự login hộ bạn.",
                title="[red]Cần đăng nhập Cici",
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
                "  2. Khởi động server: uvicorn main:app --port 8000\n"
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
                    duration: str | None = None) -> int:
    """Luồng chung cho image/video: preflight -> generate -> wait -> render."""
    if not _preflight(base, auto_launch=auto_launch):
        return api.EXIT_PREFLIGHT

    timeout = TIMEOUTS[kind]
    t0 = time.time()

    try:
        resp = api.generate(prompt, kind, base=base, model=model, references=references,
                            ratio=ratio, style=style, duration=duration)
    except api.QuotaExhausted as e:
        # server refuse vì local quota estimate = 0 (đừng lãng phí thời gian gen)
        if as_json:
            _emit_json({"status": "QUOTA_EXHAUSTED", "kind": kind, "detail": e.detail})
        else:
            out_console.print(
                Panel.fit(
                    f"[bold red]Quota {kind} đã cạn (local estimate)[/]\n"
                    f"{e.detail.get('message', '')}\n\n"
                    "[dim]Chạy `cici quota` xem chi tiết. Đừng retry ngay — "
                    "chờ reset (rolling 24h).[/]",
                    title="[red]Quota exhausted", border_style="red",
                )
            )
        return api.EXIT_QUOTA
    except ValueError as e:  # invalid model alias / prompt
        if as_json:
            _emit_json({"status": "FAILED", "error": str(e),
                        "hint": "Chạy `cici models` để xem alias hợp lệ."})
        else:
            out_console.print(f"[red]✗ {e}[/]\n[dim]Chạy `cici models` để xem alias hợp lệ.[/]")
        return api.EXIT_FAILED
    except Exception as e:  # noqa: BLE001 — server chết / mất kết nối lúc enqueue
        if as_json:
            _emit_json({"status": "ENQUEUE_ERROR", "error": str(e),
                        "hint": "core server không trả lời khi enqueue — thử lại khi server lên lại"})
        else:
            out_console.print(f"[red]Lỗi khi enqueue job: {e}[/]")
        return api.EXIT_PREFLIGHT

    job_id = resp["job_id"]
    # server trả timeout thật (theo config.yaml timing) — dùng thay vì fallback
    timeout = float(resp.get("timeout_s") or TIMEOUTS[kind])

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
        return api.EXIT_TIMEOUT
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
        return api.EXIT_PREFLIGHT

    elapsed = time.time() - t0
    status = job.get("status")

    if status == "QUOTA_EXHAUSTED":
        # Cici thực sự báo hết quota trong lúc gen
        if as_json:
            _emit_json({
                "status": "QUOTA_EXHAUSTED",
                "job_id": job_id,
                "kind": kind,
                "message": job.get("message"),
                "quota": job.get("quota"),
            })
        else:
            out_console.print(
                Panel.fit(
                    f"[bold red]Cici báo hết quota {kind}[/]\n"
                    f"[dim]{job.get('message', '')}[/]\n\n"
                    "Đừng retry ngay — chờ reset (rolling 24h).",
                    title="[red]Quota exhausted", border_style="red",
                )
            )
        return api.EXIT_QUOTA

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
        return api.EXIT_FAILED

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
        return api.EXIT_OK

    # FAILED
    if as_json:
        _emit_json({"status": "FAILED", "job_id": job_id, "error": job.get("error")})
    else:
        out_console.print(
            f"[bold red]✗ FAILED[/] · {elapsed:.1f}s\n[dim]{job.get('error')}[/]"
        )
    return api.EXIT_FAILED


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
@click.argument("prompt")
@click.option("-m", "--model", default=None, help="Model alias (xem `cici models`).")
@click.option("--ref", "refs", multiple=True,
              help="Ảnh tham chiếu (đường dẫn local). Lặp lại được, hoặc dùng dấu phẩy: --ref a.png,b.png. Tối đa 10.")
@click.option("--ratio", default=None,
              help="Tỷ lệ khung hình (xem `cici models`): 1:1, 2:3, 3:4, 4:3, 9:16, 16:9.")
@click.option("--style", default=None,
              help="Phong cách (xem `cici models`): portrait, landscape, anime, 3d, cyberpunk, oil-painting, watercolor, ...")
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.pass_context
def image(ctx: click.Context, prompt: str, model: str | None, refs: tuple[str, ...],
          ratio: str | None, style: str | None, as_json: bool):
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
                             ratio=ratio, style=style))


@main.command()
@click.argument("prompt")
@click.option("-m", "--model", default=None, help="Model alias (xem `cici models`).")
@click.option("--ref", "refs", multiple=True,
              help="Ảnh tham chiếu / frame đầu (image-to-video, đường dẫn local). Lặp lại được, hoặc dấu phẩy: --ref a.png,b.png. Tối đa 10.")
@click.option("--ratio", default=None,
              help="Tỷ lệ khung hình (xem `cici models`): 1:1, 3:4, 4:3, 9:16, 16:9, 21:9.")
@click.option("--duration", default=None, help="Thời lượng: 5s hoặc 10s.")
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.pass_context
def video(ctx: click.Context, prompt: str, model: str | None, refs: tuple[str, ...],
          ratio: str | None, duration: str | None, as_json: bool):
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
                             ratio=ratio, duration=duration))


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.pass_context
def quota(ctx: click.Context, as_json: bool):
    """Xem quota còn lại (rolling 24h local estimate + threshold đã học)."""
    base = ctx.obj["base"]
    try:
        snap = api.quota(base=base)
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
        _emit_json(snap)
        sys.exit(api.EXIT_OK)

    # human render
    for kind, info in snap.items():
        used = info.get("used_in_window", 0)
        thr = info.get("threshold")
        rmn = info.get("remaining")
        window = info.get("window_hours", 24)
        reset = info.get("reset_in_seconds")
        tbl = Table(title=f"Quota · {kind} (rolling {window}h)", show_lines=False)
        tbl.add_column("metric", style="cyan")
        tbl.add_column("value")
        tbl.add_row("used", str(used))
        tbl.add_row("threshold (auto-learn)", str(thr) if thr is not None else "[dim]chưa học (chưa từng hit limit)[/]")
        if rmn is not None:
            color = "green" if rmn > 0 else "red"
            tbl.add_row("remaining", f"[{color}]{rmn}[/{color}]")
        if reset is not None:
            hrs = reset / 3600
            tbl.add_row("reset trong", f"{hrs:.1f}h (khi gen cũ nhất roll ra khỏi window)")
        out_console.print(tbl)
        out_console.print()
    sys.exit(api.EXIT_OK)


@main.command()
@click.option("--type", "kind", type=click.Choice(["image", "video"]),
              default=None, help="Lọc theo loại (image/video).")
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.pass_context
def models(ctx: click.Context, kind: str | None, as_json: bool):
    """List các model khả dụng + generation options (ratio/style/duration)."""
    base = ctx.obj["base"]
    try:
        registry = api.models(base=base)
    except api.CiciUnreachable as e:
        # fallback: nếu server down, vẫn không list được (registry nằm trong core config)
        if as_json:
            _emit_json({"status": "unreachable", "error": str(e)})
        else:
            out_console.print(
                f"[red]✗ Core server không trả lời: {e}[/]\n"
                "[dim]`cici models` cần core server chạy (uvicorn main:app).[/]"
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
