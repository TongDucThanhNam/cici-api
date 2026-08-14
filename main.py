"""Cici API Wrapper — FastAPI server.

Architecture: Producer/Consumer + async polling.
  - POST /api/generate  -> enqueue job, return 202 + job_id immediately
  - GET  /api/status/{job_id} -> poll for COMPLETED + result URLs
  - GET  /api/health    -> Cici/CDP reachability check

Run:
    # 1. Cici must be running with CDP (see README / start_cici.bat)
    # 2. pip install fastapi "uvicorn[standard]" pyyaml playwright
    uvicorn main:app --host 127.0.0.1 --port 8000 --reload
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from cici_driver import CiciDriver, Job, JobStore, load_config
try:
    from cici import _quota
except ImportError:
    _quota = None  # type: ignore[assignment]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("cici.api")

# --------------------------------------------------------------------------- #
# Globals
# --------------------------------------------------------------------------- #
cfg = load_config("config.yaml")
JOB_QUEUE: "asyncio.Queue[Job]" = asyncio.Queue()
STORE = JobStore()
_worker_task: asyncio.Task | None = None
# Seq tăng dần khi enqueue — để /api/status tính vị trí hàng đợi (queue_ahead)
_ENQUEUE_SEQ = 0


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
    from cici_driver import run_worker

    _worker_task = asyncio.create_task(run_worker(JOB_QUEUE, STORE, cfg))
    yield
    log.info("Shutting down worker…")
    if _worker_task:
        _worker_task.cancel()


app = FastAPI(title="Cici API Wrapper", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000)
    type: Literal["image", "video"] = "image"
    model: str | None = None   # alias from config.yaml models.<type>.options[].alias
    references: list[str] = Field(default_factory=list)  # local file paths for reference upload (image + video)
    ratio: str | None = None   # alias from config.yaml options.<type>.ratios[].alias (vd "16:9")
    style: str | None = None   # alias from config.yaml options.image.styles[].alias (image only)
    duration: str | None = None  # alias from config.yaml options.video.durations[].alias (video only, "5s"/"10s")


class GenerateResponse(BaseModel):
    job_id: str
    status: str
    message: str
    timeout_s: int = 300   # gen timeout server-side cho kind này (client poll theo đây)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/api/models")
async def list_models():
    """List available models + generation options (ratios/styles/durations)."""
    return {"models": cfg.get("models", {}), "options": cfg.get("options", {})}


@app.get("/api/quota")
async def get_quota(kind: str | None = None):
    """Quota summary (rolling 24h local count + auto-learned threshold).

    ?kind=image hoặc ?kind=video để lọc.
    """
    if not _quota:
        raise HTTPException(status_code=501, detail="quota tracking unavailable (cici package missing)")
    state = _quota.load()
    return _quota.snapshot(state, kind)


@app.post("/api/generate", response_model=GenerateResponse, status_code=202)
async def generate(req: GenerateRequest):
    # validate prompt: không nhận rỗng/whitespace (Cici sẽ từ chối hoặc gen rác)
    # và null byte (làm hỏng DOM typing)
    if not req.prompt.strip():
        raise HTTPException(status_code=422, detail="Prompt không được rỗng hoặc chỉ khoảng trắng")
    if "\x00" in req.prompt:
        raise HTTPException(status_code=422, detail="Prompt chứa ký tự null không hợp lệ")
    # validate model alias if provided
    if req.model:
        registry = cfg.get("models", {}).get(req.type, {})
        valid = [o["alias"] for o in registry.get("options", [])]
        if req.model not in valid:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown model '{req.model}' for type '{req.type}'. Valid: {valid}",
            )
    # validate generation options (ratio/style/duration) against config
    opts = cfg.get("options", {}).get(req.type, {})
    if req.ratio and req.ratio not in [o["alias"] for o in opts.get("ratios", [])]:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown ratio '{req.ratio}' for type '{req.type}'. "
                   f"Valid: {[o['alias'] for o in opts.get('ratios', [])]}",
        )
    if req.style:
        if req.type != "image":
            raise HTTPException(status_code=422, detail="style chỉ hỗ trợ cho type=image")
        if req.style not in [o["alias"] for o in opts.get("styles", [])]:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown style '{req.style}' for type 'image'. "
                       f"Valid: {[o['alias'] for o in opts.get('styles', [])]}",
            )
    if req.duration:
        if req.type != "video":
            raise HTTPException(status_code=422, detail="duration chỉ hỗ trợ cho type=video")
        if req.duration not in [o["alias"] for o in opts.get("durations", [])]:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown duration '{req.duration}' for type 'video'. "
                       f"Valid: {[o['alias'] for o in opts.get('durations', [])]}",
            )
    # refuse nếu quota đã cạn (đừng lãng phí thời gian chờ fail).
    # Quota là local estimate — nếu tracking hỏng thì fail-open (vẫn cho gen).
    if _quota:
        try:
            state = _quota.load()
            rmn = _quota.remaining(state, req.type)
            if rmn is not None and rmn == 0:
                snap = _quota.snapshot(state, req.type)
                raise HTTPException(
                    status_code=429,
                    detail={
                        "message": f"Quota {req.type} đã cạn (local estimate). Thử lại sau khi reset.",
                        "quota": snap[req.type] if req.type in snap else snap,
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
              ratio=req.ratio, style=req.style, duration=req.duration)
    global _ENQUEUE_SEQ
    _ENQUEUE_SEQ += 1
    STORE.set(
        job.job_id,
        status="PENDING",
        seq=_ENQUEUE_SEQ,
        kind=job.kind,
        model=job.model or cfg["models"][req.type]["default"],
        prompt=job.prompt,
        ratio=job.ratio,
        style=job.style,
        duration=job.duration,
        created_at=job.created_at,
    )
    await JOB_QUEUE.put(job)
    log.info("Enqueued job %s (%s/%s, %d refs): %s", job.job_id, job.kind, job.model,
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
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
