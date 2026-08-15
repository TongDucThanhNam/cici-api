"""Shim backward-compat — server thật nằm ở `cici/server.py`.

Cách cũ vẫn hoạt động:  uvicorn main:app --port 8000  (chạy từ repo root).
Cách mới (self-contained):  python -m cici.server
"""
from cici.server import *  # noqa: F401,F403

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("cici.server:app", host="127.0.0.1", port=8000, reload=False)
