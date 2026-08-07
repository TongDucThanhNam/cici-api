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


class GenerateResponse(BaseModel):
    job_id: str
    status: str
    message: str


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/api/models")
async def list_models():
    """List available models per modality (from config.yaml registry)."""
    return cfg.get("models", {})


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


@app.post("/api/generate", response_model=GenerateResponse, status_code=202)
async def generate(req: GenerateRequest):
    # validate model alias if provided
    if req.model:
        registry = cfg.get("models", {}).get(req.type, {})
        valid = [o["alias"] for o in registry.get("options", [])]
        if req.model not in valid:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown model '{req.model}' for type '{req.type}'. Valid: {valid}",
            )
    job = Job(job_id=str(uuid.uuid4()), kind=req.type, prompt=req.prompt, model=req.model)
    STORE.set(
        job.job_id,
        status="PENDING",
        kind=job.kind,
        model=job.model or cfg["models"][req.type]["default"],
        prompt=job.prompt,
        created_at=job.created_at,
    )
    await JOB_QUEUE.put(job)
    log.info("Enqueued job %s (%s/%s): %s", job.job_id, job.kind, job.model, job.prompt[:60])
    return GenerateResponse(
        job_id=job.job_id,
        status="PENDING",
        message=f"Job queued. Poll GET /api/status/{job.job_id} for results.",
    )


@app.get("/api/status/{job_id}")
async def status(job_id: str):
    data = STORE.get(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    return data


@app.get("/api/jobs")
async def list_jobs():
    """Convenience: list recent jobs (debug only)."""
    return {"jobs": list(STORE.data.values())[-50:]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
