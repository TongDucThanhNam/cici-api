"""Single-consumer application service for generation jobs.

The worker depends on a small driver protocol supplied by the composition
root.  It does not import Playwright or the concrete Cici driver, which keeps
deadline/queue behavior deterministic and independently testable.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from cici.jobs import Job, JobStore, PROCESSING


log = logging.getLogger("cici.worker")


class JobDriver(Protocol):
    async def connect(self) -> None: ...

    async def execute(self, job: Job) -> dict[str, Any]: ...


DriverFactory = Callable[[dict[str, Any]], JobDriver]


def deadline_budget(cfg: dict[str, Any], kind: str) -> tuple[float, float, float]:
    """Return ``(total, generation, margin)`` for one job kind."""

    timing = cfg.get("timing", {})
    generation = timing.get(f"{kind}_timeout", 300 if kind == "image" else 600)
    margin = timing.get("hard_deadline_margin", 180)
    return generation + margin, generation, margin


async def run_worker(
    queue: "asyncio.Queue[Job]",
    store: JobStore,
    cfg: dict[str, Any],
    *,
    driver_factory: DriverFactory,
) -> None:
    """Drain jobs serially and keep the consumer alive after job failures."""

    driver = driver_factory(cfg)
    log.info("Worker starting; browser adapter connects lazily to CDP.")
    await driver.connect()
    log.info("Worker ready, waiting for jobs.")
    while True:
        job = await queue.get()
        store.set(job.job_id, status=PROCESSING, started_at=time.time())
        log.info("Processing job %s (%s)", job.job_id, job.kind)
        budget, generation_timeout, margin = deadline_budget(cfg, job.kind)
        try:
            result = await asyncio.wait_for(driver.execute(job), timeout=budget)
            store.set(job.job_id, finished_at=time.time(), **result)
        except asyncio.TimeoutError:
            log.error(
                "Job %s exceeded hard deadline %.0fs; recovering UI and marking FAILED",
                job.job_id,
                budget,
            )
            recover = getattr(driver, "recover", None)
            if recover is not None:
                await recover()
            store.set(
                job.job_id,
                status="FAILED",
                error=(
                    f"Job vượt hard deadline {budget:.0f}s "
                    f"(gen {generation_timeout:.0f}s + margin {margin:.0f}s) — "
                    "Cici có thể treo hoặc CDP mất. Thử lại job; nếu lặp lại, "
                    "restart Cici (start_cici.bat)."
                ),
                finished_at=time.time(),
            )
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the queue
            log.exception("Unhandled worker error on job %s", job.job_id)
            store.set(
                job.job_id,
                status="FAILED",
                error=str(exc),
                finished_at=time.time(),
            )
        finally:
            queue.task_done()
