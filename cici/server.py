"""Cici API Wrapper — FastAPI server.

Architecture: Producer/Consumer + async polling.
  - POST /api/generate  -> enqueue job, return 202 + job_id immediately
  - GET  /api/status/{job_id} -> poll for COMPLETED + result URLs
  - GET  /api/health    -> Cici/CDP reachability check

Run (self-contained, không cần folder repo):
    python -m cici.server            # hoặc: uvicorn cici.server:app --port 8000
"""
from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from cici import __version__, _config
from cici.driver import CiciDriver, Job, JobStore  # noqa: F401 (CiciDriver: re-export)
try:
    from cici import _quota
    from cici import _persist
except ImportError:
    _quota = None  # type: ignore[assignment]
    _persist = None  # type: ignore[assignment]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("cici.api")

# --------------------------------------------------------------------------- #
# Globals
# --------------------------------------------------------------------------- #
cfg = _config.load_config()
JOB_QUEUE: "asyncio.Queue[Job]" = asyncio.Queue()


class _PersistentJobStore(JobStore):
    """JobStore auto-persist: mỗi set() ghi ngay ra disk (best-effort).

    Tách subclass ở server.py thay vì đụng cici/driver.py — giữ driver invariant
    nguyên (cici/driver không cần biết về persistence). Fail-open: persist lỗi
    không bao giờ lan tới caller.
    """
    def set(self, job_id: str, **fields) -> None:  # type: ignore[override]
        super().set(job_id, **fields)
        try:
            if _persist is not None:
                _persist.save_jobs(self.data)
        except Exception:  # noqa: BLE001
            log.warning("Persist STORE ra disk lỗi (bỏ qua)", exc_info=True)


STORE = _PersistentJobStore()
_worker_task: asyncio.Task | None = None
# Seq tăng dần khi enqueue — để /api/status tính vị trí hàng đợi (queue_ahead)
_ENQUEUE_SEQ = 0

# Restore jobs từ ~/.cici/jobs.json nếu có. Job in-flight (PENDING/PROCESSING) → FAILED
# để agent biết cần retry. Fail-open: nếu _persist None / file corrupt → STORE rỗng.
if _persist is not None:
    _loaded = _persist.load_jobs()
    _reconciled = _persist.reconcile_on_boot(_loaded)
    if _reconciled:
        log.info("Restored %d jobs from disk; %d in-flight marked FAILED (server restarted)",
                 len(_loaded), _reconciled)
    else:
        log.info("Restored %d jobs from disk (none in-flight)", len(_loaded))
    _persist.merge_into_store(STORE.data, _loaded)
    # Đảm bảo _ENQUEUE_SEQ tiếp tục từ giá trị max đã thấy (tránh trùng seq với job cũ).
    for entry in _loaded.values():
        seq = entry.get("seq")
        if isinstance(seq, int) and seq > _ENQUEUE_SEQ:
            _ENQUEUE_SEQ = seq


def queue_ahead(store: JobStore, seq: int) -> int:
    """Số job PENDING được enqueue TRƯỚC job này (số job đứng trước trong hàng đợi)."""
    return sum(
        1 for j in store.data.values()
        if j.get("status") == "PENDING" and j.get("seq", 0) < seq
    )


# --------------------------------------------------------------------------- #
# Lifespan: start the single worker consumer on boot
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker_task
    log.info("Starting Cici consumer worker…")
    # import here to avoid circular at module load
    from cici.driver import run_worker

    _worker_task = asyncio.create_task(run_worker(JOB_QUEUE, STORE, cfg))
    # CDP warm-up probe (non-blocking, best-effort): nếu CDP chưa lên, log cảnh báo
    # sớm để user biết job đầu tiên sẽ pay full connect cost (~30s+ retry backoff).
    # Không fail nếu CDP down — server vẫn serve API, queue vẫn nhận job.
    try:
        from cici import _launcher
        alive = _launcher._cdp_alive(timeout=1.0)
        if alive:
            log.info("CDP warmup OK — Cici đang chạy, worker sẽ attach lazy.")
        else:
            log.warning("CDP chưa lên khi boot — job đầu tiên sẽ pay full connect cost. "
                        "Khởi động Cici với `--remote-debugging-port=9222` để tránh.")
    except Exception as e:  # noqa: BLE001 — probe tốt nhất là best-effort
        log.warning("CDP warmup probe lỗi (bỏ qua): %s", e)
    yield
    log.info("Shutting down worker…")
    if _worker_task:
        _worker_task.cancel()


app = FastAPI(title="Cici API Wrapper", version=__version__, lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1)  # không cap độ dài — Cici không giới hạn
    type: Literal["image", "video"] = "image"
    provider: str = "cici"   # "cici" | "doubao" — app/CDP endpoint + registry riêng
    model: str | None = None   # alias from config.yaml models.<type>.options[].alias
    references: list[str] = Field(default_factory=list)  # local file paths for reference upload (image + video)
    ratio: str | None = None   # alias from config.yaml options.<type>.ratios[].alias (vd "16:9")
    style: str | None = None   # alias from config.yaml options.image.styles[].alias (image only)
    duration: str | None = None  # alias from config.yaml options.video.durations[].alias (video only, "5s"/"10s")
    account: str | None = None  # nhãn tách quota local (user TỰ đổi account trong app — tool không tự đổi)


class GenerateResponse(BaseModel):
    job_id: str
    status: str
    message: str
    timeout_s: int = 300   # gen timeout server-side cho kind này (client poll theo đây)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/api/models")
async def list_models(provider: str = "cici"):
    """List available models + generation options (ratios/styles/durations).

    ?provider=cici|doubao — registry của provider đó (mặc định cici = legacy
    flat shape cho tương thích client cũ).
    """
    models = _provider_section("models", provider)
    options = _provider_section("options", provider)
    return {"models": models, "options": options, "provider": provider}


@app.get("/api/quota")
async def get_quota(kind: str | None = None, account: str | None = None,
                    provider: str = "cici"):
    """Quota summary (rolling 24h local count + auto-learned threshold).

    ?kind=image/video để lọc; ?account=<nhãn> cho quota riêng từng account
    (nhãn do người dùng tự gán — tool không tự đổi account);
    ?provider= đọc state quota của provider (file state tách riêng).
    """
    if not _quota:
        raise HTTPException(status_code=501, detail="quota tracking unavailable (cici package missing)")
    state = _quota.load_account(account, provider=provider)
    return _quota.snapshot(state, kind)


def _provider_section(section: str, provider: str) -> dict:
    """models/options cho provider: legacy flat = cici; provider khác nằm ở
    key trùng tên trong cùng section (models.doubao, options.doubao).

    View legacy phải loại các key provider (models.doubao...) để client cũ
    iterate kinds chỉ thấy image/video — không lộ registry provider khác."""
    data = cfg.get(section, {})
    prov_names = set(cfg.get("providers", {}) or {})
    if provider in data:
        return data[provider]
    return {k: v for k, v in data.items() if k not in prov_names}


def _validate_provider(provider: str) -> None:
    valid = list(cfg.get("providers", {}).keys()) or ["cici"]
    if provider not in valid:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown provider '{provider}'. Valid: {valid}",
        )


@app.post("/api/generate", response_model=GenerateResponse, status_code=202)
async def generate(req: GenerateRequest):
    # validate prompt: không nhận rỗng/whitespace (Cici sẽ từ chối hoặc gen rác)
    # và null byte (làm hỏng DOM typing)
    if not req.prompt.strip():
        raise HTTPException(status_code=422, detail="Prompt không được rỗng hoặc chỉ khoảng trắng")
    if "\x00" in req.prompt:
        raise HTTPException(status_code=422, detail="Prompt chứa ký tự null không hợp lệ")
    _validate_provider(req.provider)
    # validate model alias if provided (registry theo provider)
    if req.model:
        registry = _provider_section("models", req.provider).get(req.type, {})
        valid = [o["alias"] for o in registry.get("options", [])]
        if req.model not in valid:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown model '{req.model}' for type '{req.type}' "
                       f"(provider {req.provider}). Valid: {valid}",
            )
    # validate generation options (ratio/style/duration) against config
    opts = _provider_section("options", req.provider).get(req.type, {})
    if req.ratio and req.ratio not in [o["alias"] for o in opts.get("ratios", [])]:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown ratio '{req.ratio}' for type '{req.type}' "
                   f"(provider {req.provider}). "
                   f"Valid: {[o['alias'] for o in opts.get('ratios', [])]}",
        )
    if req.style:
        if req.type != "image":
            raise HTTPException(status_code=422, detail="style chỉ hỗ trợ cho type=image")
        if req.style not in [o["alias"] for o in opts.get("styles", [])]:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown style '{req.style}' for type 'image' "
                       f"(provider {req.provider}). "
                       f"Valid: {[o['alias'] for o in opts.get('styles', [])]}",
            )
    if req.duration:
        if req.type != "video":
            raise HTTPException(status_code=422, detail="duration chỉ hỗ trợ cho type=video")
        durs = [o["alias"] for o in opts.get("durations", [])]
        if not durs:
            raise HTTPException(
                status_code=422,
                detail=f"duration không khả dụng cho provider '{req.provider}' "
                       "(Doubao không có picker thời lượng riêng).",
            )
        if req.duration not in durs:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown duration '{req.duration}' for type 'video'. "
                       f"Valid: {durs}",
            )
    # account label (quota local tách theo nhãn): sanitize + từ chối nhãn lạ.
    # Tool KHÔNG đổi account — user tự đổi trong app Cici, label chỉ để đếm riêng.
    acct = _quota.sanitize_account(req.account) if _quota else None
    if req.account is not None and acct != req.account:
        raise HTTPException(
            status_code=422,
            detail=f"account label không hợp lệ ('{req.account}') — "
                   f"dùng chữ-số/_-., tối đa 32 ký tự.")
    # refuse nếu quota đã cạn (đừng lãng phí thời gian chờ fail).
    # Quota là local estimate — nếu tracking hỏng thì fail-open (vẫn cho gen).
    # Detail kèm snapshot đầy đủ (oldest_unlock_at, last_limit_type, suggested_retry_after)
    # để CLI/agent render ETA + phân loại daily vs burst.
    if _quota:
        try:
            state = _quota.load_account(acct, provider=req.provider)
            rmn = _quota.remaining(state, req.type)
            if rmn is not None and rmn == 0:
                snap = _quota.snapshot(state, req.type)
                kind_snap = snap[req.type] if req.type in snap else snap
                raise HTTPException(
                    status_code=429,
                    detail={
                        "message": (
                            f"Quota {req.type} đã cạn (local estimate). "
                            f"Thử lại sau khi reset (xem quota.oldest_unlock_at / "
                            f"suggested_retry_after)."
                        ),
                        "quota": kind_snap,
                    },
                )
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            log.warning("Quota tracking lỗi — bỏ qua quota check (fail-open)", exc_info=True)
    # validate references (image + video — Seedance hỗ trợ image-to-video)
    refs = req.references or []
    if refs:
        ref_max = cfg.get("selectors", {}).get("ref_max", 10)
        if len(refs) > ref_max:
            raise HTTPException(
                status_code=422,
                detail=f"Tối đa {ref_max} reference images, nhận được {len(refs)}",
            )
        from pathlib import Path

        def _is_valid_file(p: str) -> bool:
            # Path("") == Path(".") nên chuỗi rỗng "tồn tại" — phải chặn tay;
            # is_file() chặn thư mục; OSError/ValueError cho path dị (null byte…)
            try:
                return bool(p) and Path(p).is_file()
            except (OSError, ValueError):
                return False

        missing = [p for p in refs if not _is_valid_file(p)]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"Reference files not found (hoặc không phải file thường): {missing}",
            )
    job = Job(job_id=str(uuid.uuid4()), kind=req.type, prompt=req.prompt,
              model=req.model, references=refs,
              ratio=req.ratio, style=req.style, duration=req.duration,
              account=acct, provider=req.provider)
    global _ENQUEUE_SEQ
    _ENQUEUE_SEQ += 1
    STORE.set(
        job.job_id,
        status="PENDING",
        seq=_ENQUEUE_SEQ,
        kind=job.kind,
        provider=job.provider,
        model=job.model or _provider_section("models", req.provider)[req.type]["default"],
        prompt=job.prompt,
        ratio=job.ratio,
        style=job.style,
        duration=job.duration,
        account=job.account,
        created_at=job.created_at,
    )
    await JOB_QUEUE.put(job)
    log.info("Enqueued job %s (%s/%s/%s, %d refs): %s", job.job_id, job.provider, job.kind, job.model,
             len(job.references), job.prompt[:60])
    return GenerateResponse(
        job_id=job.job_id,
        status="PENDING",
        message=f"Job queued. Poll GET /api/status/{job.job_id} for results.",
        timeout_s=int(cfg["timing"][f"{req.type}_timeout"]),
    )


@app.get("/api/health")
async def health():
    """Probe whether Cici CDP is reachable."""
    import httpx

    cdp = cfg["cdp"]["endpoint"]
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{cdp}/json/version")
            ok = r.status_code == 200
            info = r.json() if ok else {}
        return {
            "status": "ok" if ok else "unreachable",
            "cdp_endpoint": cdp,
            "browser": info.get("Browser"),
            "queue_size": JOB_QUEUE.qsize(),
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "unreachable", "cdp_endpoint": cdp, "error": str(e)}


@app.get("/api/status/{job_id}")
async def status(job_id: str):
    data = STORE.get(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    # thêm thông tin hàng đợi để client biết phải chờ bao lâu (queue-aware timeout)
    st = data.get("status")
    return {
        **data,
        "queue_ahead": queue_ahead(STORE, data.get("seq", 0)) if st == "PENDING" else 0,
        "queue_size": JOB_QUEUE.qsize(),
    }


@app.get("/api/jobs")
async def list_jobs():
    """Convenience: list recent jobs (debug only)."""
    return {"jobs": list(STORE.data.values())[-50:]}


if __name__ == "__main__":
    import sys as _sys

    import uvicorn

    host, port = "127.0.0.1", 8000
    argv = _sys.argv[1:]
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    uvicorn.run("cici.server:app", host=host, port=port, reload=False)
