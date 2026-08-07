"""cici CLI — gen ảnh/video qua app Cici (Dola Browser).

Thin client gọi cici-api core server. Cần core server + Cici chạy trước.

Exit codes (cho AI agent phân biệt):
    0 = COMPLETED        1 = FAILED (job)
    2 = TIMEOUT          3 = PREFLIGHT (server/Cici chưa chạy)
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

console = Console(stderr=True)  # human-facing log -> stderr, stdout giữ JSON/URLs sạch cho agent
out_console = Console()         # stdout (cho URLs / JSON)

# Timeout theo loại (giây) — khớp config.yaml core
TIMEOUTS = {"image": 320, "video": 620}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _preflight(base: str) -> bool:
    """Check core server + Cici reachable. Trả True nếu OK."""
    try:
        h = api.health(base=base)
    except api.CiciUnreachable as e:
        out_console.print(
            Panel.fit(
                f"[bold red]Core server không trả lời[/]\n"
                f"endpoint: {base}\nlỗi: {e}\n\n"
                "[bold]Cách khắc phục:[/]\n"
                "  1. Khởi động Cici có CDP:\n"
                "       start_cici.bat\n"
                "     (hoặc: Cici.exe --remote-debugging-port=9222 --user-data-dir=<User Data>)\n"
                "  2. Khởi động core API server:\n"
                "       uvicorn main:app --port 8000\n"
                "  3. Chạy lại lệnh này.",
                title="[red]Preflight failed",
                border_style="red",
            )
        )
        return False
    if h.get("status") != "ok":
        out_console.print(
            Panel.fit(
                f"[bold red]Cici CDP không nối được[/]\n"
                f"health trả: {h}\n\n"
                "[bold]Cách khắc phục:[/]\n"
                "  Cici phải chạy với flag --remote-debugging-port=9222.\n"
                "  Chạy start_cici.bat (tự kill + relaunch Cici có CDP).",
                title="[red]Preflight failed",
                border_style="red",
            )
        )
        return False
    return True


def _emit_json(obj: dict) -> None:
    """In JSON sạch ra stdout (agent parse được)."""
    out_console.print_json(_json.dumps(obj, ensure_ascii=False, default=str))


def _render_result(job: dict, elapsed: float) -> None:
    """In kết quả COMPLETED dạng bảng có màu (human-friendly)."""
    urls = job.get("result_urls") or []
    kind = job.get("kind", "?")
    out_console.print()
    out_console.print(
        f"[bold green]✓ COMPLETED[/] · {kind} · {elapsed:.1f}s · {len(urls)} kết quả"
    )
    if not urls:
        out_console.print("[yellow](không có URL kết quả)[/]")
        return
    tbl = Table(title="Kết quả", show_lines=False)
    tbl.add_column("#", style="dim", width=3)
    tbl.add_column("URL", overflow="fold")
    tbl.add_column("Hết hạn", style="cyan")
    for i, u in enumerate(urls, 1):
        secs = api.seconds_until_expiry(u)
        if secs is None:
            exp = "—"
        else:
            local = api.expiry_local(u)
            exp = f"{local:%H:%M:%S} ({secs/3600:.1f}h nữa)"
            if secs < 3600:
                exp = f"[bold red]{exp}[/]"
        tbl.add_row(str(i), u, exp)
    out_console.print(tbl)
    # cảnh báo expiry ngắn
    soon = [u for u in urls if (api.seconds_until_expiry(u) or 1e9) < 3600]
    if soon:
        out_console.print(
            "[bold red]⚠ URL sắp hết hạn (<1h) — download ngay nếu cần.[/]"
        )


def _run_generation(prompt: str, kind: str, as_json: bool, base: str,
                    model: str | None = None) -> int:
    """Luồng chung cho image/video: preflight -> generate -> wait -> render."""
    if not _preflight(base):
        return api.EXIT_PREFLIGHT

    timeout = TIMEOUTS[kind]
    t0 = time.time()

    try:
        job_id = api.generate(prompt, kind, base=base, model=model)
    except ValueError as e:  # invalid model alias
        out_console.print(f"[red]✗ {e}[/]\n[dim]Chạy `cici models` để xem alias hợp lệ.[/]")
        return api.EXIT_FAILED
    except Exception as e:  # noqa: BLE001
        out_console.print(f"[red]Lỗi khi enqueue job: {e}[/]")
        return api.EXIT_FAILED

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

    elapsed = time.time() - t0
    status = job.get("status")

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
@click.pass_context
def main(ctx: click.Context, base: str):
    """Gen ảnh/video qua app Cici (Dola Browser).

    Cần core server (cici-api) + Cici đang chạy. Chạy `cici health` để check.
    """
    ctx.ensure_object(dict)
    ctx.obj["base"] = base
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.pass_context
def health(ctx: click.Context, as_json: bool):
    """Check core server + Cici CDP reachable."""
    base = ctx.obj["base"]
    try:
        h = api.health(base=base)
    except api.CiciUnreachable as e:
        if as_json:
            _emit_json({"status": "unreachable", "error": str(e)})
        else:
            out_console.print(f"[red]✗ Core server không trả lời: {e}[/]")
        sys.exit(api.EXIT_PREFLIGHT)
    if as_json:
        _emit_json(h)
    else:
        ok = h.get("status") == "ok"
        icon = "[green]✓[/]" if ok else "[red]✗[/]"
        out_console.print(
            f"{icon} core={h.get('status')} · browser={h.get('browser')} · "
            f"queue={h.get('queue_size')} · {base}"
        )
    sys.exit(api.EXIT_OK if h.get("status") == "ok" else api.EXIT_PREFLIGHT)


@main.command()
@click.argument("prompt")
@click.option("-m", "--model", default=None, help="Model alias (xem `cici models`).")
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.pass_context
def image(ctx: click.Context, prompt: str, model: str | None, as_json: bool):
    """Sinh ảnh từ PROMPT (block tới xong, ~2-3 phút).

    Model mặc định: seedream-5-pro. Đổi bằng -m/--model (xem `cici models`).
    """
    sys.exit(_run_generation(prompt, "image", as_json, ctx.obj["base"], model=model))


@main.command()
@click.argument("prompt")
@click.option("-m", "--model", default=None, help="Model alias (xem `cici models`).")
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.pass_context
def video(ctx: click.Context, prompt: str, model: str | None, as_json: bool):
    """Sinh video từ PROMPT (block tới xong).

    Model mặc định: seedance-2.5. LƯU Ý: core chưa detect <video>, có thể timeout.
    """
    sys.exit(_run_generation(prompt, "video", as_json, ctx.obj["base"], model=model))


@main.command()
@click.option("--type", "kind", type=click.Choice(["image", "video"]),
              default=None, help="Lọc theo loại (image/video).")
@click.option("--json", "as_json", is_flag=True, help="Xuất JSON thay vì text màu.")
@click.pass_context
def models(ctx: click.Context, kind: str | None, as_json: bool):
    """List các model khả dụng (cho --model flag)."""
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

    if kind:
        registry = {kind: registry.get(kind, {})}
    if as_json:
        _emit_json(registry)
        sys.exit(api.EXIT_OK)

    # human table
    for modality, info in registry.items():
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
                out_console.print(f"  {u}")
        if s.get("error"):
            out_console.print(f"[dim]  err: {s['error']}[/]")
    sys.exit(api.EXIT_OK if st == "COMPLETED" else (api.EXIT_FAILED if st == "FAILED" else api.EXIT_OK))


if __name__ == "__main__":
    main()
