"""End-to-end test against the running API server.
Start the server first:  uvicorn main:app --port 8000

This posts one image job, polls /api/status until done, prints the result URLs.
"""
import sys
import time
import httpx

BASE = "http://127.0.0.1:8000"
PROMPT = "một chú chó corgi mặc áo mưa đỏ, phong cách hoạt hình 3D, mưa nhẹ"


def main():
    with httpx.Client(timeout=10) as c:
        # health
        h = c.get(f"{BASE}/api/health").json()
        print("health:", h)
        if h["status"] != "ok":
            print("Cici CDP not reachable — start Cici + the server first.")
            sys.exit(1)

        # enqueue
        r = c.post(
            f"{BASE}/api/generate",
            json={"prompt": PROMPT, "type": "image"},
        ).json()
        job_id = r["job_id"]
        print(f"submitted job {job_id}; polling…")

        # poll
        t0 = time.time()
        last = None
        while time.time() - t0 < 320:
            s = c.get(f"{BASE}/api/status/{job_id}").json()
            if s["status"] != last:
                print(f"  T+{int(time.time()-t0)}s status={s['status']}")
                last = s["status"]
            if s["status"] in ("COMPLETED", "FAILED"):
                print("\n=== RESULT ===")
                print(s)
                return
            time.sleep(4)
        print("Timed out waiting for job.")


if __name__ == "__main__":
    main()
