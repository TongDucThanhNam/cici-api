# Cici API Wrapper

Bọc app **Cici / Dola Browser** (ByteDance) thành một REST API để gen **ảnh / video** qua code — tận dụng quota free của account đã đăng nhập trong app.

Cách hoạt động: Playwright nối vào Cici đang chạy qua **Chrome DevTools Protocol (CDP)**, điều khiển UI như người thật, queue xử lý tuần tự (vì Cici chỉ có 1 ô chat). **Đã test E2E**: gen 1 ảnh ~140 giây, trả về 4 URL.

> ⚠️ Lưu ý: đây là automation UI, không phải API chính chủ. Nó vi phạm tinh thần ToS của Cici, brittle khi UI đổi, và bị giới hạn bởi quota tài khoản. Để chạy production nên dùng [Volcengine Doubao API](https://www.volcengine.com/product/doubao) thay thế.

## Yêu cầu

- **Cici / Dola Browser** đã cài + **đã đăng nhập** account ByteDance của bạn (mỗi người dùng quota account riêng).
- **Python 3.10+**.
- Windows (có `start_cici.bat`); macOS/Linux chạy được nếu tự khởi động Cici với flag CDP.

## Quick Start (3 bước)

```bash
# 1. Cài (một lần)
git clone <repo-url> cici-api && cd cici-api
powershell -ExecutionPolicy Bypass -File install.ps1   # Windows
# bash install.sh                                       # macOS/Linux/Git Bash

# 2. Mở Cici app + đăng nhập account ByteDance của bạn (thủ công, 1 lần)
#    (nếu Cici chưa mở khi gọi cici, CLI sẽ TỰ khởi động nó có CDP)

# 3. Dùng — chỉ 1 lệnh!
cici health                                    # check trạng thái
cici image "mèo orange dễ thương" -m seedream-4.5
```

> 💡 **Auto-launch**: mặc định CLI tự khởi động Cici (có CDP) + core server ngầm nếu chưa chạy.
> Bạn chỉ cần mở Cici + login thủ công 1 lần (vì login cần account/mật khẩu của bạn).
> Tắt auto bằng `--no-auto-launch`.



---

## Kiến trúc

```
Client  ──POST /api/generate──▶  FastAPI  ──▶  asyncio.Queue  ──▶  Worker (1 luồng)
        ◀──202 + job_id──┘            │                                  │
                                       │                                  ▼
Client  ──GET /api/status/{id}──▶  JobStore  ◀── update kết quả ──  Playwright (CDP → Cici)
```

**Producer/Consumer + Async Polling** — lý do: gen ảnh mất 2–5 phút, không thể block HTTP request. N requests tới cũng xếp hàng, xử lý tuần tự để không giẫm đạp UI.

---

## Cài đặt (chi tiết)

Cách nhanh: chạy `install.ps1` (Windows) hoặc `install.sh` (macOS/Linux) — nó tự cài deps + package + thêm `cici` vào PATH.

Cách thủ công:
```bash
pip install -r requirements.txt   # deps core + CLI
pip install -e .                   # cài package cici-cli (lệnh `cici`)
```
Đã test với Python 3.14, Playwright 1.61, FastAPI 0.141.

---

## Chạy

### 1. Khởi động Cici với CDP

Cách A — chạy script (tự kill + relaunch + chờ port):
```cmd
start_cici.bat
```

Cách B — thủ công:
```cmd
:: Tắt Cici trước (tránh instance cũ không có CDP)
taskkill /F /IM Cici.exe

:: Khởi động với remote debugging + giữ nguyên login session
"%LOCALAPPDATA%\Cici\Application\app\Cici.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%LOCALAPPDATA%\Cici\User Data"
```

Verify CDP lên:
```bash
curl http://127.0.0.1:9222/json/version
```
→ phải trả `200` + `Browser: Chrome/147...`

**Để cửa sổ Cici mở.** Không được tắt.

### 2. Khởi động API server

```bash
cd cici-api
uvicorn main:app --host 127.0.0.1 --port 8000
```

Log phải hiện: `Worker ready, waiting for jobs.` — nếu không, worker đang thử reconnect CDP (chờ Cici lên).

### 3. Dùng API

**Gen ảnh:**
```bash
curl -X POST http://127.0.0.1:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt":"một con mèo orange dễ thương, phong cách chibi","type":"image"}'
```
→ `{"job_id":"...", "status":"PENDING"}` (HTTP 202)

**Poll kết quả:**
```bash
curl http://127.0.0.1:8000/api/status/<job_id>
```
Trạng thái: `PENDING → PROCESSING → COMPLETED` (kèm `result_urls[]`) hoặc `FAILED`.

**Check sức khỏe:**
```bash
curl http://127.0.0.1:8000/api/health
```

**Test tự động (gen 1 ảnh rồi poll tới xong):**
```bash
python test_e2e.py
```

---

## CLI (`cici`) — dành cho AI Coding Agents & người

Bên cạnh raw HTTP API, có một **CLI pip package** (`cici-cli`) — thin client gọi core server. Đây là cách **AI coding agents dễ dùng nhất**: gọi 1 lệnh = xong (sync), có `--json` để parse, exit code rõ ràng để agent tự rẽ nhánh.

### Cài đặt

```bash
cd cici-api
pip install -e .
```
→ tạo lệnh `cici`.

**Lỗi `cici not recognized` trong PowerShell?** pip cài `cici.exe` vào user Scripts dir (`%APPDATA%\Python\Python314\Scripts`) vốn không nằm trong PATH. Fix 1 lần (persistent):
```powershell
$s = "$env:APPDATA\Python\Python314\Scripts"
$p = [Environment]::GetEnvironmentVariable('PATH','User')
if ($p -notlike "*$s*") { [Environment]::SetEnvironmentVariable('PATH', "$p;$s", 'User') }
```
Sau đó **mở terminal mới** (terminal cũ đã cache PATH). Hoặc gọi tạm: `%APPDATA%\Python\Python314\Scripts\cici.exe`.

### Dùng

```bash
# Check core + Cici đang chạy (chạy trước mọi lệnh gen)
cici health
cici health --json          # JSON cho agent

# Xem các model khả dụng (cho --model)
cici models                 # bảng text
cici models --type image    # chỉ image
cici models --json          # JSON cho agent

# Gen ảnh — block tới xong (~30s-3 phút tuỳ model)
cici image "mèo orange dễ thương, phong cách chibi"
cici image "..." -m seedream-4.5     # chọn model
cici image "..." --json     # JSON: {status, job_id, kind, model, elapsed_s, urls:[...]}

# Gen video (LƯU Ý: core chưa detect <video>, có thể timeout)
cici video "thuyền buồm trên biển lúc hoàng hôn"
cici video "..." -m seedance-1.0

# Poll trạng thái 1 job (không block)
cici status <job_id>
```

### Models

| Loại | Alias (`--model`) | Tên | Ghi chú |
|---|---|---|---|
| image | `seedream-5-pro` *(default)* | Seedream 5.0 Pro | chất lượng cao nhất |
| image | `seedream-4.5` | Seedream 4.5 | nhanh hơn (~40s vs ~140s) |
| video | `seedance-2.5` *(default)* | Dreamina Seedance 2.5 | chất lượng tốt nhất |
| video | `seedance-2-fast` | Dreamina Seedance 2.0 Nhanh | nhanh |
| video | `seedance-1.0` | Dreamina Seedance 1.0 | đơn giản |

Registry nằm trong `config.yaml` (`models:` section). Khi Cici thêm model → chạy `python inspect_dom.py` re-check + cập nhật config.

### Quota tracking

Cici free có giới hạn gen hằng ngày nhưng **không tiết lộ** số còn lại / khi reset. Tool **tự track local**:

- **`cici quota`** — xem số đã dùng (rolling 24h), threshold đã học, còn lại, khi reset
- **Auto-learn threshold** — khi bạn hit limit lần đầu, tool ghi nhớ "N gen thì hết" → từ đó cảnh báo + **từ chối gen trước khi tốn thời gian chờ fail** (exit 4)
- **Rolling 24h** — quota window trượt, phản ánh đúng cách ByteDance limit (gen cũ tự drop khỏi count sau 24h)

```bash
cici quota                  # xem cả image + video
cici quota --json           # JSON cho agent
```

State lưu ở `~/.cici/quota.json`. Xoá file đó để reset.

### Exit codes (để AI agent rẽ nhánh)

| Code | Ý nghĩa | Khi nào |
|---|---|---|
| `0` | COMPLETED | gen xong, có URLs |
| `1` | FAILED | job COMPLETED với lỗi server-side |
| `2` | TIMEOUT | gen không xong trong timeout (320s ảnh / 620s video) |
| `3` | PREFLIGHT | core server hoặc Cici chưa chạy → CLI in hướng dẫn khắc phục |
| `4` | QUOTA_EXHAUSTED | hết quota hằng ngày — **đừng retry ngay**, chờ reset (rolling 24h) |

### Agent integration note

Khi agent gọi `cici image`, lệnh **block ~2-3 phút**. Agent cần set tool timeout cao (≥ 320s) hoặc dùng `cici image --json` và đọc stdout. URL kết quả có `x-expires` (parse sẵn trong `--json` field `expires_local`) — nhưng giá trị thường xa 10 năm nên không lo sớm hết hạn.

Override core URL (mặc định `http://127.0.0.1:8000`):
```bash
cici --base http://other-host:8000 image "..."
# hoặc:  export CICI_API=http://other-host:8000
```


---

## Endpoints

| Method | Path | Mô tả |
|---|---|---|
| `GET`  | `/api/health` | Cici CDP reachable? + queue size |
| `POST` | `/api/generate` | Enqueue job (`{prompt, type: "image"|"video"}`) → 202 + `job_id` |
| `GET`  | `/api/status/{job_id}` | Trạng thái + kết quả |
| `GET`  | `/api/jobs` | List job gần đây (debug) |

---

## Cấu hình

Toàn bộ selector + timeout nằm trong **`config.yaml`**. Khi Cici đổi UI (đổi class / testid), **chỉ sửa file này** — không đụng code.

```yaml
selectors:
  skill_image: 'button[data-testid="skill_bar_button_3"]'
  skill_video: 'button[data-testid="skill_bar_button_17"]'
  editor_prose: 'div.tiptap.ProseMirror'   # input ở skill mode
  send_button: '[data-testid="chat_input_send_button"]'
  done_indicator: '[data-testid="message_action_bar"]'  # xuất hiện = gen xong
  result_image: '[data-testid="mdbox_image"] img'

timing:
  image_timeout: 300   # giây
  video_timeout: 600
```

---

## Cách né "hố bom"

(Đã hiện thực hóa trong `cici_driver.py`)

1. **DOM mutability** — selector tách ra `config.yaml`. Đổi UI → sửa config.
2. **Zombie state** — mỗi job có timeout; fail thì `page.reload()` clear state, đánh dấu `FAILED`, đi tiếp job kế.
3. **Concurrency đua UI** — worker **1 luồng** (Cici chỉ có 1 ô chat). Tăng worker = crash.
4. **Mất kết nối CDP** — `_attach()` tự reconnect với backoff lũy thừa (2s → 30s).
4. **HTTP timeout** — không block request chờ gen; trả 202 ngay, client poll.
5. **State mất khi restart server** — `JobStore` in-memory. Production: đổi sang Redis.

---

## Giới hạn đã biết

- **Video gen chưa test E2E** — selector (`skill_bar_button_17` + `<video>`) đã có sẵn trong config, logic `_wait_result` hiện chỉ bắt ảnh. Để hỗ trợ video, thêm nhánh detect `<video src>` trong `cici_driver._wait_result`.
- **Tốc độ** — tuần tự, ~2 phút/job ảnh. Không phù hợp throughput cao.
- **Quota** — dùng quota free của account Cici; rate-limit / block có thể xảy ra.
- **Stability** — UI Cici cập nhật = phải re-inspect config.

---

## File map

```
cici-api/
├── main.py            # CORE: FastAPI endpoints + lifespan (start worker)
├── cici_driver.py     # CORE: Playwright CDP driver + worker loop (consumer)
├── config.yaml        # CORE: selectors + model registry (sửa khi UI đổi)
├── start_cici.bat     # Launcher Cici có CDP (thủ công — CLI cũng tự làm)
├── test_e2e.py        # Smoke test raw API
├── inspect_dom.py     # (dev) re-inspect DOM chat khi UI đổi
├── pyproject.toml     # CLI package metadata + entry point `cici`
├── requirements.txt   # Python deps (core + CLI)
├── install.ps1        # One-click installer (Windows)
├── install.sh         # One-click installer (macOS/Linux)
├── LICENSE            # MIT
├── cici/              # CLI package (thin HTTP client → core)
│   ├── __init__.py
│   ├── _client.py     # httpx client + URL expiry parser
│   ├── _launcher.py   # auto-launch Cici + spawn server
│   ├── _quota.py      # rolling 24h quota tracker + auto-learn threshold
│   └── cli.py         # Click commands: health/image/video/models/quota/status
└── README.md
```

