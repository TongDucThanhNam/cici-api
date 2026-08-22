"""Hermetic contract tests for the refactored architecture boundaries."""
from __future__ import annotations

import asyncio
import contextlib
import unittest

from cici.catalog import ConfigCatalog
from cici.devtools import build_command
from cici.jobs import Job, JobStore, queue_ahead
from cici.worker import run_worker


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = ConfigCatalog(
            {
                "providers": {"cici": {}, "doubao": {}},
                "models": {
                    "image": {
                        "default": "base",
                        "options": [{"alias": "base", "select_text": "Base"}],
                    },
                    "doubao": {
                        "image": {
                            "default": "db",
                            "options": [{"alias": "db", "select_text": "DB"}],
                        }
                    },
                },
                "options": {
                    "image": {"ratios": [{"alias": "1:1"}]},
                    "doubao": {"image": {"ratios": [{"alias": "auto"}]}},
                },
            }
        )

    def test_legacy_and_nested_provider_views_are_isolated(self) -> None:
        self.assertNotIn("doubao", self.catalog.section("models", "cici"))
        self.assertEqual(
            self.catalog.resolve_model("image", None, "cici")["alias"],
            "base",
        )
        self.assertEqual(
            self.catalog.resolve_model("image", None, "doubao")["alias"],
            "db",
        )
        self.assertEqual(
            self.catalog.aliases("options", "image", "ratios", "doubao"),
            ["auto"],
        )


class JobTests(unittest.TestCase):
    def test_queue_ahead_counts_only_earlier_pending_jobs(self) -> None:
        store = JobStore()
        store.set("a", status="PENDING", seq=1)
        store.set("b", status="PROCESSING", seq=2)
        store.set("c", status="PENDING", seq=3)
        self.assertEqual(queue_ahead(store, 3), 1)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_uses_injected_driver_and_completes_job(self) -> None:
        instances: list[_FakeDriver] = []

        def factory(cfg: dict) -> _FakeDriver:
            driver = _FakeDriver(cfg)
            instances.append(driver)
            return driver

        queue: "asyncio.Queue[Job]" = asyncio.Queue()
        store = JobStore()
        task = asyncio.create_task(
            run_worker(queue, store, {"timing": {}}, driver_factory=factory)
        )
        await queue.put(Job(job_id="j1", kind="image", prompt="x"))
        await asyncio.wait_for(queue.join(), timeout=1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        self.assertTrue(instances[0].connected)
        self.assertEqual(store.get("j1")["status"], "COMPLETED")  # type: ignore[index]


class DevtoolTests(unittest.TestCase):
    def test_ast_tree_progressive_commands(self) -> None:
        self.assertEqual(
            build_command("map", [], executable="sg"),
            ["sg", "outline", "cici", "--items", "structure", "--view", "names"],
        )
        self.assertEqual(
            build_command(
                "show",
                ["cici/driver.py"],
                executable="sg",
                symbol="CiciDriver",
            ),
            [
                "sg",
                "outline",
                "cici/driver.py",
                "--items",
                "all",
                "--view",
                "expanded",
                "--match",
                "CiciDriver",
            ],
        )
        self.assertEqual(
            build_command("scan", [], executable="sg"),
            ["sg", "scan", "."],
        )


class _FakeDriver:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def execute(self, job: Job) -> dict:
        return {"status": "COMPLETED", "kind": job.kind, "result_urls": []}

    async def recover(self, timeout: float = 30.0) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
