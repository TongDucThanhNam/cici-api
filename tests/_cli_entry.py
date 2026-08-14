"""Entry cho stress test chạy CLI trong process thật (tests/stress_test.py dùng).

Patch _launcher để CLI không tự khởi động Cici/uvicorn thật, rồi gọi cici CLI
bình thường. stdout của process này chính là stdout agent thật sẽ đọc.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cici import _launcher  # noqa: E402

_launcher._cdp_alive = lambda timeout=2.0: True
_launcher._api_alive = lambda timeout=2.0: True
_launcher.check_login = lambda timeout=5.0: (True, "mock logged-in")
_launcher.ensure_cici = lambda log=print, cdp_timeout=30.0: (True, "mock")
_launcher.ensure_server = lambda log=print, cwd=None, api_timeout=20.0: (True, "mock")

from cici import cli  # noqa: E402

# stress test có thể thu nhỏ timeout image/video để test nhanh (giây)
if os.environ.get("CICI_STRESS_TIMEOUT"):
    t = float(os.environ["CICI_STRESS_TIMEOUT"])
    cli.TIMEOUTS["image"] = t
    cli.TIMEOUTS["video"] = t

if __name__ == "__main__":
    main_args = sys.argv[1:]
    cli.main.main(args=main_args, standalone_mode=True)
