"""Domain objects and status contracts for queued generation jobs.

This module deliberately has no FastAPI, Click, persistence, or Playwright
dependencies.  API, worker, client, and persistence adapters can therefore
share the job vocabulary without importing browser automation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


PENDING = "PENDING"
PROCESSING = "PROCESSING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
CONTENT_BLOCKED = "CONTENT_BLOCKED"

IN_FLIGHT_STATUSES = (PENDING, PROCESSING)
TERMINAL_STATUSES = (COMPLETED, FAILED, QUOTA_EXHAUSTED, CONTENT_BLOCKED)


@dataclass
class Job:
    """Validated work item consumed by the single browser worker."""

    job_id: str
    kind: str
    prompt: str
    model: str | None = None
    references: list[str] = field(default_factory=list)
    ratio: str | None = None
    style: str | None = None
    duration: str | None = None
    account: str | None = None
    provider: str = "cici"
    created_at: float = field(default_factory=time.time)


@dataclass
class JobStore:
    """Small in-memory status port; the server adds persistence via subclassing."""

    data: dict[str, dict[str, Any]] = field(default_factory=dict)

    def set(self, job_id: str, **fields: Any) -> None:
        self.data.setdefault(job_id, {}).update(fields)

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        return self.data.get(job_id)


def queue_ahead(store: JobStore, seq: int) -> int:
    """Return the number of earlier pending jobs in the single-consumer queue."""

    return sum(
        1
        for job in store.data.values()
        if job.get("status") == PENDING and job.get("seq", 0) < seq
    )
