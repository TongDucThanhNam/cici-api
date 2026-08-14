"""Stress test cici-api KHÔNG cần Cici / KHÔNG tốn quota.

Chiến lược: chạy FastAPI app thật (main.app) + worker loop thật
(cici_driver.run_worker) trong process này qua uvicorn trên loopback port,
chỉ thay CiciDriver bằng FakeDriver scriptable (ok/fail/hang/quota/blocked).
Toàn bộ HTTP, queue, JobStore, seq, CLI exit codes là code thật.

    python tests/stress_test.py

Scenario nào phát hiện bug thật của product → in dòng "FINDING" (kèm mô tả)
nhưng không tính là fail của harness — findings được tổng hợp cuối file.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# repo root: main.py load config.yaml theo CWD
REPO = Path(__file__).resolve().parent.parent
os.chdir(REPO)
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
import yaml  # noqa: E402
from click.testing import CliRunner  # noqa: E402

import cici_driver  # noqa: E402
from cici import _client, _quota  # noqa: E402

# giảm noise log khi pipe stdout
import logging  # noqa: E402
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

FINDINGS: list[str] = []
PASSES = 0
FAILS = 0


def ok(name: str) -> None:
    global PASSES
    PASSES += 1
    print(f"PASS   {name}")


def fail(name: str, detail: str) -> None:
    global FAILS
    FAILS += 1
    print(f"FAIL   {name}: {detail}")


def finding(name: str, detail: str) -> None:
    FINDINGS.append(f"{name}: {detail}")
    print(f"FINDING {name}: {detail}")


def check(name: str, cond: bool, detail: str = "") -> bool:
    if cond:
        ok(name)
    else:
        fail(name, detail)
    return cond


# --------------------------------------------------------------------------- #
# FakeDriver — thay CiciDriver; run_worker thật vẫn chạy nguyên vẹn
# --------------------------------------------------------------------------- #
LONG_URL = "https://cdn.example.com/rc_gen_image/" + "a" * 260 + ".jpeg?x-expires=4102444800"


class MockController:
    """Điều khiển hành vi FakeDriver từ test thread (an toàn qua GIL)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self, duration: float = 0.03) -> None:
        self.duration = duration
        self.script: list[str] = []      # FIFO modes; hết thì dùng default_mode
        self.default_mode = "ok"
        self.fail_rate = 0.0
        self.rng = random.Random(42)
        self.release_hang = False
        self.processed = 0
        self.raised = 0
        self.modes_seen: list[str] = []

    def next_mode(self) -> str:
        if self.script:
            m = self.script.pop(0)
        elif self.fail_rate and self.rng.random() < self.fail_rate:
            m = "fail"
        else:
            m = self.default_mode
        self.modes_seen.append(m)
        return m


CTL = MockController()
MOCK_CDP_URL = ""  # set bởi patch_launcher_for_mock_server()


class FakeDriver:
    """Cùng contract với CiciDriver.execute: trả result dict hoặc raise."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    async def connect(self) -> None:
        return

    async def execute(self, job) -> dict:
        mode = CTL.next_mode()
        if mode == "ok":
            await asyncio.sleep(CTL.duration)
            CTL.processed += 1
            return {
                "status": "COMPLETED",
                "result_urls": ["https://cdn.example.com/ok1.jpeg?x-expires=4102444800", LONG_URL],
                "kind": job.kind,
                "model": job.model or "mock-default",
            }
        if mode == "fail":
            CTL.raised += 1
            raise RuntimeError(f"mock failure for {job.job_id}")
        if mode == "quota":
            return {"status": "QUOTA_EXHAUSTED", "kind": job.kind,
                    "message": "Bạn đã đạt đến giới hạn tạo hình ảnh", "quota": None}
        if mode == "blocked":
            return {"status": "CONTENT_BLOCKED", "kind": job.kind,
                    "message": "Để bảo vệ bản quyền, tôi không thể hiển thị..."}
        if mode == "hang":
            # giả lập driver kẹt nhưng CÒN có thể recover (un-hang bằng cờ)
            while not CTL.release_hang:
                await asyncio.sleep(0.02)
            CTL.processed += 1
            return {"status": "COMPLETED", "result_urls": ["https://cdn.example.com/post-hang.jpeg"],
                    "kind": job.kind, "model": "mock"}
        if mode == "cdp_down":
            # giả lập bounded _attach đã bỏ cuộc: ConnectionError rõ ràng
            CTL.raised += 1
            raise ConnectionError(
                "Cici CDP (http://127.0.0.1:9222) không nối được sau 90s: connect refused. "
                "Kiểm tra Cici đang chạy với --remote-debugging-port=9222 (start_cici.bat).")
        if mode == "stuck":
            # giả lập driver treo ngoài _wait_result (ignore cả release_hang)
            # → chỉ hard deadline của run_worker cứu được queue
            await asyncio.sleep(3600)
            CTL.processed += 1
            return {"status": "COMPLETED", "result_urls": [], "kind": job.kind, "model": "mock"}
        raise AssertionError(f"unknown mode {mode}")


cici_driver.CiciDriver = FakeDriver  # noqa: monkeypatch cho cả main lifespan lẫn run_worker


# --------------------------------------------------------------------------- #
# Server thật trên port ngẫu nhiên
# --------------------------------------------------------------------------- #
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self) -> None:
        import main  # noqa: import sau khi chdir repo root
        self.main = main
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        cfg = uvicorn.Config(main.app, host="127.0.0.1", port=self.port, log_level="error")
        self.srv = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.srv.run, daemon=True)

    def start(self, timeout: float = 15.0) -> None:
        self.thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.srv.started:
                return
            time.sleep(0.05)
        raise RuntimeError("uvicorn không start được")

    def stop(self, timeout: float = 10.0) -> None:
        self.srv.should_exit = True
        self.thread.join(timeout=timeout)


def http() -> httpx.Client:
    return httpx.Client(base_url=SRV.base, timeout=30.0)


def post_generate(client: httpx.Client, **fields) -> httpx.Response:
    payload = {"prompt": fields.pop("prompt", "stress test"), "type": fields.pop("type", "image")}
    payload.update(fields)
    return client.post("/api/generate", json=payload)


def wait_terminal(client: httpx.Client, job_id: str, timeout: float = 60.0) -> dict:
    """Poll tới trạng thái terminal (không dùng _client.wait_status để bắt 5xx nếu có)."""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        r = client.get(f"/api/status/{job_id}")
        if r.status_code != 200:
            return {"_http": r.status_code}
        last = r.json()
        if last.get("status") in ("COMPLETED", "FAILED", "QUOTA_EXHAUSTED", "CONTENT_BLOCKED"):
            return last
        time.sleep(0.02)
    return {"_timeout": True, "last": last}


def drain(ctl_expected: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and CTL.processed + CTL.raised < ctl_expected:
        time.sleep(0.05)


# --------------------------------------------------------------------------- #
# S1: Burst enqueue 100 job đồng thời + drain
# --------------------------------------------------------------------------- #
def s1_burst() -> None:
    print("\n--- S1: burst 100 POST /api/generate đồng thời ---")
    CTL.reset(duration=0.02)
    SRV.main.STORE.data.clear()
    base_seq = max((d.get("seq", 0) for d in SRV.main.STORE.data.values()), default=0)

    def one(i: int) -> tuple[int, int, str]:
        with http() as c:
            r = post_generate(c, prompt=f"burst {i}")
            jid = r.json().get("job_id", "") if r.status_code == 202 else ""
            return i, r.status_code, jid

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=25) as ex:
        results = list(ex.map(one, range(100)))
    elapsed = time.time() - t0

    codes = [c for _, c, _ in results]
    ids = [j for _, _, j in results]
    check("S1a 100 concurrent enqueue đều 202", all(c == 202 for c in codes),
          f"codes={sorted(set(codes))}")
    check("S1b job_id duy nhất", len(set(ids)) == 100, f"unique={len(set(ids))}")

    drain(100, timeout=60)
    with http() as c:
        finals = [wait_terminal(c, j) for j in ids]
        seqs = [f.get("seq") for f in finals]
        statuses = [f.get("status") for f in finals]
    check("S1c 100 job đều COMPLETED", statuses.count("COMPLETED") == 100,
          f"statuses={ {s: statuses.count(s) for s in set(statuses)} }")
    check("S1d seq duy nhất & > baseline", len(set(seqs)) == 100 and all(s and s > base_seq for s in seqs),
          f"unique={len(set(seqs))}")
    check(f"S1e enqueue 100 job trong {elapsed:.2f}s (server không block)", elapsed < 10, f"{elapsed:.2f}s")


# --------------------------------------------------------------------------- #
# S2: Status hammering trong lúc worker đang xử lý
# --------------------------------------------------------------------------- #
def s2_status_hammer() -> None:
    print("\n--- S2: 8 thread dập /api/status + /api/health + /api/jobs khi queue đang chạy ---")
    CTL.reset(duration=0.05)
    stop = threading.Event()
    errors: list[str] = []
    counts = {"200": 0, "other": 0}

    def hammer(i: int) -> None:
        with http() as c:
            while not stop.is_set():
                try:
                    path = ["/api/health", "/api/jobs", "/api/quota"][i % 3] if i >= 5 else f"/api/status/{ids[i % len(ids)]}"
                    r = c.get(path)
                    if r.status_code == 200:
                        counts["200"] += 1
                    else:
                        counts["other"] += 1
                        errors.append(f"{path} -> {r.status_code}")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{path} -> {e!r}")

    with http() as c:
        rs = [post_generate(c, prompt=f"hammer {i}") for i in range(20)]
        ids = [r.json()["job_id"] for r in rs]

    threads = [threading.Thread(target=hammer, args=(i,), daemon=True) for i in range(8)]
    for t in threads:
        t.start()
    drain(20, timeout=60)
    time.sleep(0.5)  # thêm ~1s hammer sau khi drain
    stop.set()
    for t in threads:
        t.join(timeout=5)

    check("S2a status/health/jobs hammering không có lỗi/5xx",
          not errors, f"errors[:3]={errors[:3]}")
    check("S2b số request thành công > 0", counts["200"] > 50, f"counts={counts}")


# --------------------------------------------------------------------------- #
# S3: Adversarial / malformed input — mọi case phải 4xx, không bao giờ 5xx
# --------------------------------------------------------------------------- #
def s3_adversarial() -> None:
    print("\n--- S3: adversarial inputs (kỳ vọng 4xx, cấm 5xx) ---")
    CTL.reset()
    tmpdir = Path(REPO / "__stress_tmp__")
    tmpdir.mkdir(exist_ok=True)

    cases: list[tuple[str, dict, bytes | None, dict | None, bool]] = []
    # (tên, json fields, raw body, headers, is_finding_case)
    cases.append(("prompt rỗng", {"prompt": "", "type": "image"}, None, None, False))
    cases.append(("prompt chỉ whitespace", {"prompt": "   ", "type": "image"}, None, None, True))
    cases.append(("prompt 2001 ký tự", {"prompt": "x" * 2001, "type": "image"}, None, None, False))
    cases.append(("prompt unicode+emoji", {"prompt": "画像 🌸 Đẹp lắm «todo» ‍quote-mark ‌‍.", "type": "image"}, None, None, False))
    cases.append(("prompt null byte", {"prompt": "abc\u0000def", "type": "image"}, None, None, True))
    cases.append(("type lạ", {"prompt": "ok", "type": "audio"}, None, None, False))
    cases.append(("type chữ hoa", {"prompt": "ok", "type": "IMAGE"}, None, None, False))
    cases.append(("model không tồn tại", {"prompt": "ok", "type": "image", "model": "gpt-4o"}, None, None, False))
    cases.append(("model đúng tên nhưng sai kind", {"prompt": "ok", "type": "video", "model": "seedream-5-pro"}, None, None, False))
    cases.append(("ratio lạ", {"prompt": "ok", "type": "image", "ratio": "21:9"}, None, None, False))
    cases.append(("style cho video", {"prompt": "ok", "type": "video", "style": "anime"}, None, None, False))
    cases.append(("duration cho image", {"prompt": "ok", "type": "image", "duration": "5s"}, None, None, False))
    cases.append(("duration lạ", {"prompt": "ok", "type": "video", "duration": "5h"}, None, None, False))
    cases.append(("11 references", {"prompt": "ok", "type": "image",
                                    "references": [str(tmpdir / "f.png")] + ["c:/nonexistent.png"] * 10}, None, None, False))
    cases.append(("reference không tồn tại", {"prompt": "ok", "type": "image", "references": ["c:/definitely/missing.png"]}, None, None, False))
    cases.append(("reference là thư mục", {"prompt": "ok", "type": "image", "references": [str(REPO)]}, None, None, True))
    cases.append(("reference chuỗi rỗng", {"prompt": "ok", "type": "image", "references": [""]}, None, None, False))
    cases.append(("body không phải JSON", {}, b"not json at all {{{", {"Content-Type": "application/json"}, False))
    cases.append(("body là JSON list", {}, b"[1,2,3]", {"Content-Type": "application/json"}, False))
    cases.append(("body 2MB prompt", {"prompt": "y" * 2_000_000, "type": "image"}, None, None, False))
    cases.append(("prompt là số", {"prompt": 12345, "type": "image"}, None, None, False))
    cases.append(("references là chuỗi", {"prompt": "ok", "type": "image", "references": "a.png"}, None, None, False))

    findings_expected = {c[0] for c in cases if c[4]}
    # input hợp lệ bắt buộc phải được chấp nhận (202)
    must_accept = {"prompt unicode+emoji"}
    findings_expected -= must_accept

    bad_5xx: list[str] = []
    accepted_findings: list[str] = []
    accepted_all: list[str] = []
    rejected_must_accept: list[str] = []

    with http() as c:
        for name, fields, raw, headers, _ in cases:
            if raw is not None:
                r = c.post("/api/generate", content=raw, headers=headers or {})
            else:
                r = post_generate(c, **fields)
            if r.status_code >= 500:
                bad_5xx.append(f"{name} -> {r.status_code}: {r.text[:120]}")
            elif r.status_code == 202:
                accepted_all.append(name)
                if name in findings_expected:
                    accepted_findings.append(name)
            elif name in must_accept:
                rejected_must_accept.append(f"{name} -> {r.status_code}")
        # status endpoint với job id dị
        for jid in ["../../etc/passwd", "a" * 5000, "job-🌸-%00", "%2e%2e%2f"]:
            r = c.get(f"/api/status/{jid}")
            if r.status_code >= 500:
                bad_5xx.append(f"status {jid[:20]} -> {r.status_code}")
        # quota endpoint kind lạ
        r = c.get("/api/quota", params={"kind": "banana"})
        if r.status_code >= 500:
            bad_5xx.append(f"quota kind=banana -> {r.status_code}")

    check("S3a không có response 5xx nào", not bad_5xx, "; ".join(bad_5xx[:3]))
    check("S3b prompt unicode/emoji được chấp nhận (tính năng, không phải lỗi)",
          not rejected_must_accept, str(rejected_must_accept))
    unexpected_accepts = [a for a in accepted_all if a in findings_expected]
    for a in unexpected_accepts:
        finding("S3", f"server CHẤP NHẬN input đáng ngờ (202 thay vì 422): {a}")
    if not unexpected_accepts:
        ok("S3c các input đáng ngờ đều bị từ chối đúng")


# --------------------------------------------------------------------------- #
# S4: Worker bền trước fail 30% (fail injection)
# --------------------------------------------------------------------------- #
def s4_fail_injection() -> None:
    print("\n--- S4: 60 job, 30% raise bất ngờ — worker phải sống tiếp ---")
    CTL.reset(duration=0.01)
    CTL.fail_rate = 0.3
    with http() as c:
        rs = [post_generate(c, prompt=f"inj {i}") for i in range(60)]
        ids = [r.json()["job_id"] for r in rs]
    drain(60, timeout=60)
    # fail_rate chỉ áp dụng khi script rỗng; chờ tất cả terminal
    with http() as c:
        finals = [wait_terminal(c, j) for j in ids]
    statuses = [f.get("status") for f in finals]
    n_failed = statuses.count("FAILED")
    n_done = statuses.count("COMPLETED")
    check("S4a 60 job đều đạt trạng thái terminal (không kẹt)", len(finals) == 60 and all(statuses), f"{statuses.count(None)} missing")
    check(f"S4b mix FAILED({n_failed})/COMPLETED({n_done}) — worker sống qua {n_failed} exceptions",
          n_failed > 5 and n_done > 20, f"failed={n_failed} done={n_done}")
    check("S4c job FAILED có error message", all("error" in (f or {}) for f in finals if f.get("status") == "FAILED"), "")


# --------------------------------------------------------------------------- #
# S5: Hang job chiếm worker → queue starving; client timeout E2E
# --------------------------------------------------------------------------- #
def s5_hang_and_starvation() -> None:
    print("\n--- S5: 1 job treo (driver kẹt) + 3 job xếp sau — starvation + timeout E2E ---")
    CTL.reset()
    CTL.script = ["hang", "ok", "ok", "ok"]
    with http() as c:
        rs = [post_generate(c, prompt=f"s5 {i}") for i in range(4)]
        ids = [r.json()["job_id"] for r in rs]
        hang_id, tail_ids = ids[0], ids[1:]

        # chờ hang job vào PROCESSING
        deadline = time.time() + 10
        while time.time() < deadline:
            s = c.get(f"/api/status/{hang_id}").json()
            if s.get("status") == "PROCESSING":
                break
            time.sleep(0.05)
        time.sleep(0.3)
        tail = [c.get(f"/api/status/{j}").json() for j in tail_ids]
        check("S5a khi driver kẹt, 3 job sau đứng PENDING (single-consumer giữ nguyên)",
              all(t.get("status") == "PENDING" for t in tail),
              f"{[t.get('status') for t in tail]}")
        qa = [t.get("queue_ahead") for t in tail]
        # hang job đang PROCESSING nên không được đếm; job i có i job PENDING đứng trước
        check("S5b queue_ahead tăng dần 0,1,2 (job đứng sau chờ lâu hơn)", qa == [0, 1, 2], f"queue_ahead={qa}")

        # client thật (cici._client) chờ hang job với timeout ngắn → TimeoutError E2E qua HTTP
        t0 = time.time()
        try:
            _client.wait_status(hang_id, timeout=0.5, poll_interval=0.1, base=SRV.base)
            fail("S5c wait_status phải raise TimeoutError", "đã return")
        except TimeoutError as e:
            ok(f"S5c wait_status raise TimeoutError đúng ({time.time()-t0:.1f}s, msg có PROCESSING: {'PROCESSING' in str(e)})")

        # thả hang → 3 job sau hoàn thành
        CTL.release_hang = True
        for j in tail_ids:
            f = wait_terminal(c, j, timeout=30)
            if f.get("status") != "COMPLETED":
                fail("S5d tail job hoàn thành sau khi hang được thả", f"{j}: {f}")
                return
        ok("S5d 3 job sau hoàn thành đúng thứ tự sau khi un-hang")
        fh = wait_terminal(c, hang_id, timeout=5)
        check("S5e hang job cũng COMPLETED sau un-hang", fh.get("status") == "COMPLETED", f"{fh}")
    CTL.release_hang = False


# --------------------------------------------------------------------------- #
# S6: API contract — models/quota/health/jobs + 404
# --------------------------------------------------------------------------- #
def s6_contract() -> None:
    print("\n--- S6: API contract cơ bản ---")
    with http() as c:
        r = c.get("/api/models")
        m = r.json()
        check("S6a /api/models trả đủ 2 kind + options",
              r.status_code == 200 and set(m.get("models", {})) == {"image", "video"}
              and "ratios" in m.get("options", {}).get("image", {}), str(m)[:100])
        r = c.get("/api/quota")
        check("S6b /api/quota 200 (đọc quota.json thật, read-only)", r.status_code == 200, str(r.status_code))
        r = c.get("/api/quota", params={"kind": "image"})
        check("S6c /api/quota?kind=image chỉ trả image", r.status_code == 200 and "image" in r.json(), "")
        r = c.get("/api/status/00000000-0000-0000-0000-000000000000")
        check("S6d job không tồn tại -> 404", r.status_code == 404, str(r.status_code))


# --------------------------------------------------------------------------- #
# S7: CLI end-to-end (Click runner) — exit codes + JSON stdout parse được
# --------------------------------------------------------------------------- #
def patch_launcher_for_mock_server() -> None:
    """CLI preflight không được phép launch Cici/uvicorn thật trong stress test.

    Bao gồm cả mock CDP HTTP server: /api/health của app probe CDP thật ở
    127.0.0.1:9222 — trỏ endpoint sang mock để không đụng session người dùng.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class CDPHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/json/version":
                body = json.dumps({"Browser": "stress-cdp-mock/1.0"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):  # noqa: N802
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), CDPHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # main.health() đọc cfg lúc request-time → trỏ sang mock
    global MOCK_CDP_URL
    MOCK_CDP_URL = f"http://127.0.0.1:{port}"
    SRV.main.cfg["cdp"]["endpoint"] = MOCK_CDP_URL

    from cici import _launcher
    _launcher._cdp_alive = lambda timeout=2.0: True
    _launcher._api_alive = lambda timeout=2.0: True
    _launcher.check_login = lambda timeout=5.0: (True, "mock logged-in")
    _launcher.ensure_cici = lambda log=print, cdp_timeout=30.0: (True, "mock")
    _launcher.ensure_server = lambda log=print, cwd=None, api_timeout=20.0: (True, "mock")


def try_parse_json_stdout(out: str):
    """stdout của `--json` phải parse được bởi agent (không rich-wrap)."""
    try:
        return True, json.loads(out)
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def s7_cli_matrix() -> None:
    print("\n--- S7: CLI exit-code matrix + JSON stdout contract (subprocess thật) ---")
    patch_launcher_for_mock_server()
    import subprocess

    def run_cli(args: list[str], cli_timeout_override: float | None = None,
                wall: float = 180.0) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        if cli_timeout_override is not None:
            env["CICI_STRESS_TIMEOUT"] = str(cli_timeout_override)
        return subprocess.run(
            [sys.executable, "-u", str(REPO / "tests" / "_cli_entry.py"),
             "--base", SRV.base] + args,
            capture_output=True, text=True, timeout=wall, env=env,
        )

    # a) COMPLETED -> exit 0 + stdout là JSON parse được (đúng như agent đọc pipe)
    CTL.reset(duration=0.05)
    r = run_cli(["image", "stress cli ok", "--json"])
    pk, p = try_parse_json_stdout(r.stdout)
    if check("S7a `cici image --json` exit 0 khi COMPLETED", r.returncode == 0,
             f"exit={r.returncode} err={r.stderr[-200:] if r.stderr else ''}"):
        if pk and p.get("status") == "COMPLETED" and p.get("urls") and p["urls"][0].get("expires_unix"):
            ok("S7b stdout JSON parse được: COMPLETED + urls + expires")
        else:
            finding("S7", f"`--json` stdout KHÔNG parse được: {str(p)[:150]} | "
                          f"repr: {r.stdout[:160]!r}")

    # b) FAILED -> exit 1
    CTL.reset()
    CTL.script = ["fail"]
    r = run_cli(["image", "stress cli fail", "--json"])
    check("S7c `cici image --json` exit 1 khi FAILED", r.returncode == 1, f"exit={r.returncode}")

    # c) QUOTA_EXHAUSTED (trạng thái sau gen) -> kỳ vọng exit 4 NGAY.
    # Nếu wait_status không coi đây là terminal, CLI poll hết timeout rồi báo
    # nhầm TIMEOUT (exit 2) — bug ảnh hưởng trực tiếp khách hàng.
    CTL.reset()
    CTL.script = ["quota"]
    t0 = time.time()
    r = run_cli(["image", "stress cli quota", "--json"], cli_timeout_override=3.0)
    dt = time.time() - t0
    if r.returncode == 4 and dt < 5.0:
        ok("S7d exit 4 ngay khi QUOTA_EXHAUSTED")
    else:
        finding("S7d", f"QUOTA_EXHAUSTED: CLI exit={r.returncode} sau {dt:.0f}s (kỳ vọng exit 4 ngay). "
                       "cici/_client.wait_status KHÔNG coi QUOTA_EXHAUSTED là terminal → "
                       "khách hit daily-limit chờ đủ timeout (320s/620s) rồi nhận nhầm TIMEOUT.")

    # d) CONTENT_BLOCKED -> exit 1 ngay
    CTL.reset()
    CTL.script = ["blocked"]
    t0 = time.time()
    r = run_cli(["image", "stress cli blocked", "--json"], cli_timeout_override=3.0)
    dt = time.time() - t0
    pk, p = try_parse_json_stdout(r.stdout)
    if r.returncode == 1 and pk and p.get("status") == "CONTENT_BLOCKED" and dt < 5.0:
        ok("S7e exit 1 ngay khi CONTENT_BLOCKED + JSON đúng status")
    else:
        finding("S7e", f"CONTENT_BLOCKED: CLI exit={r.returncode} sau {dt:.0f}s (kỳ vọng exit 1 ngay) — "
                       "cùng nguyên nhân wait_status thiếu trạng thái terminal.")

    # e) TIMEOUT -> exit 2 + JSON TIMEOUT
    # CLI giờ lấy timeout từ server (timeout_s trong 202) → thu nhỏ server-side
    CTL.reset()
    CTL.script = ["hang"]
    orig_img_to = SRV.main.cfg["timing"]["image_timeout"]
    SRV.main.cfg["timing"]["image_timeout"] = 1
    try:
        r = run_cli(["image", "stress cli timeout", "--json"], wall=60)
    finally:
        SRV.main.cfg["timing"]["image_timeout"] = orig_img_to
        CTL.release_hang = True
        time.sleep(0.3)
        CTL.release_hang = False
    pk, p = try_parse_json_stdout(r.stdout)
    check("S7f exit 2 khi TIMEOUT + JSON parse được",
          r.returncode == 2 and pk and p.get("status") == "TIMEOUT",
          f"exit={r.returncode} parsed={p if not pk else ''}")

    # f) model sai -> exit 1 (không crash)
    r = run_cli(["image", "x", "-m", "does-not-exist", "--json"])
    check("S7g exit 1 khi model alias sai", r.returncode == 1, f"exit={r.returncode}")

    # g) 429 refuse tại enqueue (patch remaining=0) -> exit 4
    orig_remaining = _quota.remaining
    _quota.remaining = lambda state, kind, now=None: 0
    try:
        r = run_cli(["image", "quota refuse", "--json"])
        check("S7h exit 4 khi server refuse 429", r.returncode == 4, f"exit={r.returncode}")
    finally:
        _quota.remaining = orig_remaining

    # h) lệnh phụ: models / quota / status
    for args, want in [(["models", "--json"], 0), (["quota", "--json"], 0),
                       (["status", "no-such-job", "--json"], 1)]:
        r = run_cli(args)
        pk, _p = try_parse_json_stdout(r.stdout)
        check(f"S7i `cici {args[0]}` exit {want} + JSON parse",
              r.returncode == want and pk, f"exit={r.returncode} json_ok={pk}")

    # i) human mode (không --json): URL dài phải nằm trọn trên 1 dòng để copy/download được
    CTL.reset(duration=0.05)
    r = run_cli(["image", "stress cli human"])
    if r.returncode == 0:
        url_intact = any(LINE.strip() == LONG_URL for LINE in r.stdout.splitlines())
        if url_intact:
            ok("S7j human mode: URL dài in nguyên vẹn 1 dòng")
        else:
            finding("S7j", "human mode (không --json): URL dài bị rich FOLD sang nhiều dòng — "
                           "khách copy URL bị thiếu ký tự. (rich Console mặc định wrap theo width 80.)")

    # k) 3 CLI process song song (kịch bản nhiều agent) — queue-aware timeout
    CTL.reset(duration=0.3)
    procs = [subprocess.Popen(
        [sys.executable, "-u", str(REPO / "tests" / "_cli_entry.py"),
         "--base", SRV.base, "image", f"parallel {i}", "--json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for i in range(3)]
    outs = [p.communicate(timeout=120) for p in procs]
    codes = [p.returncode for p in procs]
    json_all = True
    for (o, e) in outs:
        pk, _p = try_parse_json_stdout(o)
        if not pk:
            json_all = False
    check("S7k 3 `cici image` process song song: exit 0 hết + JSON parse",
          all(c == 0 for c in codes) and json_all, f"codes={codes}")


# --------------------------------------------------------------------------- #
# S8: Server chết giữa chừng khi CLI đang poll — có traceback hay exit code sạch?
# --------------------------------------------------------------------------- #
def s8_server_death_midpoll() -> None:
    print("\n--- S8: server chết giữa lúc CLI poll kết quả ---")
    # app fresh trên port riêng để giết không ảnh hưởng server chính
    spec = importlib.util.find_spec("main")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    fresh.cfg["cdp"]["endpoint"] = MOCK_CDP_URL  # health phải ok để CLI qua preflight
    port2 = free_port()
    cfg = uvicorn.Config(fresh.app, host="127.0.0.1", port=port2, log_level="error")
    srv2 = uvicorn.Server(cfg)
    th2 = threading.Thread(target=srv2.run, daemon=True)
    th2.start()
    base2 = f"http://127.0.0.1:{port2}"
    deadline = time.time() + 15
    while time.time() < deadline and not srv2.started:
        time.sleep(0.05)

    try:
        # S8a: job chạy chậm (4s), giết server NGAY SAU khi job vào PROCESSING —
        # CLI đang ở giữa wait_status → kỳ vọng POLL_ERROR JSON + exit 3, sạch.
        CTL.reset(duration=4.0)
        with httpx.Client(base_url=base2, timeout=10) as c:
            r = post_generate(c, prompt="s8a")
            job_id = r.json()["job_id"]
        cli_proc = subprocess.Popen(
            [sys.executable, "-u", str(REPO / "tests" / "_cli_entry.py"),
             "--base", base2, "image", "s8a midpoll death", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # chờ CLI kịp enqueue job của nó (server2 có 2 jobs) rồi +0.5s cho CLI
        # bắt đầu poll — tránh race kill trước khi CLI enqueue
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                jobs = httpx.get(f"{base2}/api/jobs", timeout=3).json().get("jobs", [])
                if len(jobs) >= 2:
                    break
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.2)
        time.sleep(0.5)
        srv2.should_exit = True
        th2.join(timeout=10)
        time.sleep(0.3)
        try:
            out, err = cli_proc.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            cli_proc.kill()
            out, err = cli_proc.communicate()
            fail("S8a CLI kết thúc khi server chết giữa poll", "timeout 90s — CLI treo")
            return
        pk, p = try_parse_json_stdout(out)
        has_traceback = "Traceback (most recent call last)" in (err or "")
        if pk and p.get("status") == "POLL_ERROR" and cli_proc.returncode == 3 and not has_traceback:
            ok("S8a server chết giữa poll: exit 3 + JSON POLL_ERROR sạch (không traceback)")
        else:
            finding("S8a", f"server chết giữa poll: exit={cli_proc.returncode}, "
                           f"JSON={'PARSE_FAIL' if not pk else p.get('status')}, traceback={has_traceback} — "
                           "kỳ vọng exit 3 + POLL_ERROR (agent tự recover bằng `cici status`).")

        # S8b: `status` sau khi server chết — lỗi sạch
        res = subprocess.run(
            [sys.executable, "-u", str(REPO / "tests" / "_cli_entry.py"),
             "--base", base2, "status", job_id, "--json"],
            capture_output=True, text=True, timeout=60,
        )
        pk, p = try_parse_json_stdout(res.stdout)
        has_traceback = "Traceback (most recent call last)" in (res.stderr or "")
        if has_traceback or (not pk):
            finding("S8b", f"`cici status` sau khi server chết: traceback/JSON hỏng "
                           f"(exit={res.returncode}, traceback={has_traceback})")
        else:
            ok(f"S8b status khi server chết: exit={res.returncode}, JSON sạch ({str(p)[:60]})")
    finally:
        if th2.is_alive():
            srv2.should_exit = True
            th2.join(timeout=5)


# --------------------------------------------------------------------------- #
# S9: Driver CDP-loss — _attach có bound? queue có stall khi CDP mất?
# --------------------------------------------------------------------------- #
def s9_cdp_loss_deadlock() -> None:
    print("\n--- S9: mất CDP giữa chừng — driver bỏ cuộc đúng hạn, queue không stall ---")
    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))

    # rebuild class CiciDriver THẬT từ source (module đã bị monkeypatch FakeDriver)
    ns: dict = {}
    try:
        code = (REPO / "cici_driver.py").read_text(encoding="utf-8")
        exec(compile(code, "cici_driver.py", "exec"), ns)  # noqa: S102 — đọc chính repo
        RealDriver = ns["CiciDriver"]
    except Exception as e:  # noqa: BLE001
        check("S9 setup (exec cici_driver source)", False, str(e))
        return

    # S9a: _attach phải BỎ CUỘC sau connect_timeout (không retry vô hạn nữa)
    cfg2 = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    cfg2["cdp"]["connect_timeout"] = 1.0
    cfg2["cdp"]["reconnect_initial_delay"] = 0.2
    cfg2["cdp"]["reconnect_max_delay"] = 0.5
    d = RealDriver(cfg2)

    class DeadPW:
        class chromium:
            @staticmethod
            async def connect_over_cdp(*a, **k):
                raise ConnectionRefusedError("CDP down (mock)")

        async def stop(self):
            return

    d._pw = DeadPW()  # type: ignore[assignment]
    box: dict = {}
    loop = asyncio.new_event_loop()

    def runner() -> None:
        try:
            loop.run_until_complete(d._attach())
            box["result"] = "returned"
        except BaseException as e:  # noqa: BLE001
            box["exc"] = f"{type(e).__name__}: {e}"

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    th.join(timeout=8.0)
    try:
        if th.is_alive():
            finding("S9a", "CiciDriver._attach() vẫn retry VÔ HẠN khi CDP mất (8s chưa bỏ cuộc) — "
                           "job sẽ kẹt PROCESSING + block queue.")
            loop.call_soon_threadsafe(loop.stop)
            th.join(timeout=1.0)
        else:
            exc = box.get("exc", "")
            if "ConnectionError" in exc:
                ok(f"S9a _attach bỏ cuộc đúng hạn: ConnectionError ({exc[:70]}…)")
            else:
                fail("S9a _attach bỏ cuộc đúng loại lỗi", f"got {box}")
    finally:
        try:
            loop.close()
        except RuntimeError:
            pass

    # S9b: CDP mất khi xử lý → job FAILED NHANH với lỗi rõ ràng, job kế tiếp vẫn chạy
    # (run_worker thật + FakeDriver mode cdp_down — giả lập bounded _attach đã từ chối)
    CTL.reset()
    CTL.script = ["cdp_down", "ok"]

    async def scenario():
        q: "asyncio.Queue" = asyncio.Queue()
        store = cici_driver.JobStore()
        await q.put(cici_driver.Job(job_id="j-cdp", kind="image", prompt="x"))
        await q.put(cici_driver.Job(job_id="j-next", kind="image", prompt="y"))
        task = asyncio.create_task(cici_driver.run_worker(q, store, cfg))
        s1 = s2 = None
        for _ in range(400):  # tối đa 20s
            await asyncio.sleep(0.05)
            s1, s2 = store.get("j-cdp"), store.get("j-next")
            t1 = (s1 or {}).get("status") in ("COMPLETED", "FAILED", "QUOTA_EXHAUSTED", "CONTENT_BLOCKED")
            t2 = (s2 or {}).get("status") in ("COMPLETED", "FAILED", "QUOTA_EXHAUSTED", "CONTENT_BLOCKED")
            if t1 and t2:
                break
        task.cancel()
        return s1, s2

    t0 = time.time()
    s1, s2 = asyncio.run(scenario())
    dt = time.time() - t0
    st1, st2 = (s1 or {}).get("status"), (s2 or {}).get("status")
    if st1 == "FAILED" and st2 == "COMPLETED" and dt < 20:
        has_cdp_hint = "CDP" in str((s1 or {}).get("error", ""))
        ok(f"S9b CDP mất → job1 FAILED sau {dt:.1f}s (lỗi nêu rõ CDP: {has_cdp_hint}), "
           "job2 vẫn COMPLETED — queue không stall")
    else:
        finding("S9b", f"CDP mất: job1={st1}, job2={st2} sau {dt:.0f}s — kỳ vọng FAILED + COMPLETED nhanh "
                       "(queue phải tự đi tiếp, không PENDING vô hạn).")


# --------------------------------------------------------------------------- #
# S10: quota.json corrupt / sai type → server sống sót?
# --------------------------------------------------------------------------- #
def s10_quota_corruption() -> None:
    print("\n--- S10: quota.json bị corrupt (ghi nửa chừng / tay sửa sai) ---")
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="cici_stress_"))

    good = tmp / "good.json"
    good.write_text(json.dumps({"history": {"image": [time.time()], "video": []},
                                "threshold": {"image": 5, "video": None}}), encoding="utf-8")
    st = _quota.load(good)
    check("S10a quota.json hợp lệ load OK", _quota.count_recent(st, "image") == 1, "")

    bad_json = tmp / "bad.json"
    bad_json.write_text('{"history": {"image": [1,2,', encoding="utf-8")  # nửa chừng
    st = _quota.load(bad_json)
    check("S10b JSON đứt giữa chừng -> dùng default (không crash)", st.history["image"] == [], "")

    wrong_type = tmp / "wrong.json"
    wrong_type.write_text('{"history": {"image": "not-a-list"}, "threshold": {"image": "abc"}}', encoding="utf-8")
    st2 = _quota.load(wrong_type)  # from_dict không validate
    try:
        _quota.remaining(st2, "image")
        ok("S10c sai type trong quota.json vẫn sống (có validate)")
    except Exception as e:  # noqa: BLE001
        finding("S10c", f"quota.json sai type -> exception lan tới /api/generate → HTTP 500 ({type(e).__name__}: {e}). "
                        "from_dict không validate; load() không bắt OSError/TypeError.")

    # S10d: server có 500 thật không khi load() ném lỗi? patch load để trỏ file lỗi
    if any("S10c" in f for f in FINDINGS):
        orig_load = _quota.load
        _quota.load = lambda path=None: orig_load(wrong_type)  # type: ignore[assignment]
        try:
            with http() as c:
                r = post_generate(c, prompt="quota corrupt probe")
                if r.status_code >= 500:
                    finding("S10d", f"POST /api/generate trả {r.status_code} khi quota.json sai type — "
                                    "mọi lệnh gen đều 500 cho tới khi tay sửa file.")
                else:
                    ok(f"S10d server chịu được quota corrupt (HTTP {r.status_code})")
        finally:
            _quota.load = orig_load


# --------------------------------------------------------------------------- #
# S11: Hard deadline — driver treo ngoài _wait_result → queue vẫn đi tiếp
# --------------------------------------------------------------------------- #
def s11_hard_deadline() -> None:
    print("\n--- S11: driver treo (không thể un-hang) → hard deadline cứu queue ---")
    CTL.reset()
    CTL.script = ["stuck", "ok"]
    orig_img = SRV.main.cfg["timing"]["image_timeout"]
    orig_margin = SRV.main.cfg["timing"].get("hard_deadline_margin", 180)
    SRV.main.cfg["timing"]["image_timeout"] = 1
    SRV.main.cfg["timing"]["hard_deadline_margin"] = 1  # budget = 2s
    try:
        with http() as c:
            rs = [post_generate(c, prompt=f"s11 {i}") for i in range(2)]
            ids = [r.json()["job_id"] for r in rs]
            f1 = wait_terminal(c, ids[0], timeout=30)
            f2 = wait_terminal(c, ids[1], timeout=30)
        st1, st2 = f1.get("status"), f2.get("status")
        if st1 == "FAILED" and "hard deadline" in str(f1.get("error", "")) and st2 == "COMPLETED":
            ok("S11 job treo bị hard deadline đánh FAILED (lỗi rõ ràng), job sau COMPLETED")
        else:
            finding("S11", f"job treo: st1={st1} (err={str(f1.get('error'))[:80]}), st2={st2} — "
                           "kỳ vọng FAILED chứa 'hard deadline' + COMPLETED (queue không stall vĩnh viễn).")
    finally:
        SRV.main.cfg["timing"]["image_timeout"] = orig_img
        SRV.main.cfg["timing"]["hard_deadline_margin"] = orig_margin
        time.sleep(0.2)  # cho worker hoàn tất recover


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
SRV: Server  # type: ignore[valid-type]


def main() -> int:
    global SRV
    t0 = time.time()
    print("Khởi động server thật (uvicorn + worker thật + FakeDriver)…")
    SRV = Server()
    SRV.start()
    try:
        s1_burst()
        s2_status_hammer()
        s3_adversarial()
        s4_fail_injection()
        s5_hang_and_starvation()
        s6_contract()
        s7_cli_matrix()
        s8_server_death_midpoll()
        s9_cdp_loss_deadlock()
        s10_quota_corruption()
        s11_hard_deadline()
    finally:
        CTL.release_hang = True
        SRV.stop()
        for p in Path(REPO).glob("__stress_tmp__"):
            try:
                p.rmdir()
            except OSError:
                pass

    print(f"\n{'='*70}")
    print(f"Stress xong trong {time.time()-t0:.1f}s — PASS {PASSES} / FAIL {FAILS} / FINDINGS {len(FINDINGS)}")
    if FINDINGS:
        print("\nFINDINGS (bug/sơ hở thật của product phát hiện qua stress test):")
        for i, f in enumerate(FINDINGS, 1):
            print(f"  {i}. {f}")
    sys.stdout.flush()
    sys.stderr.flush()
    rc = 1 if FAILS else 0
    # thoát cứng: các thread daemon (loop _attach retry) có thể crash interpreter
    # lúc shutdown làm mất buffer stdout trên Python 3.14/Windows
    os._exit(rc)


if __name__ == "__main__":
    sys.exit(main())
