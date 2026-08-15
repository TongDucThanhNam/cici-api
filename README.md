<div align="center">

# 🎨 Cici API Wrapper

**Gen ảnh / video qua app Cici (Dola Browser) bằng 1 lệnh** — CLI + local API server cho AI coding agents.

[![Stars][stars-shield]][stars-url]
[![Forks][forks-shield]][forks-url]
[![Issues][issues-shield]][issues-url]
[![License MIT][license-shield]][license-url]
<br/>
[![Python 3.10+][python-shield]][python-url]
[![FastAPI][fastapi-shield]][fastapi-url]
[![Playwright][playwright-shield]][playwright-url]
[![Windows auto-launch][windows-shield]][windows-url]

[Quickstart](#quickstart) · [CLI](#cli-cici) · [Exit codes](#exit-codes) · [HTTP API](#http-api) · [Cấu hình](#cấu-hình) · [Xử lý sự cố](#xử-lý-sự-cố) · [Giới hạn](#giới-hạn-đã-biết) · [Phát triển](#phát-triển)

</div>

> ⚠️ **Đây là UI automation, không phải API chính chủ.** Tool điều khiển app Cici
> đang đăng nhập của bạn qua CDP. Nó vi phạm tinh thần ToS của Cici, brittle khi
> UI đổi, và bị giới hạn bởi quota tài khoản. Để chạy production nghiêm túc, hãy
> dùng API chính chủ của ByteDance (Volcano Engine Ark / Doubao).

---

## Tính năng

- **1 lệnh là xong**: `cici image "prompt"` — tự khởi động Cici (có CDP) + server nếu thiếu, block tới khi có kết quả.
- **Cho AI agent**: `--json` stdout sạch, exit codes chuẩn hoá (0/1/2/3/4), timeout đồng bộ từ server.
- **Ảnh gốc full-size** — tự nâng từ preview ~288px lên bản gốc (vd 1773×2364) qua image viewer.
- **Image + Video**: model Seedream/Seedance, ratio, style, duration, ảnh tham chiếu (image-to-video).
- **Queue an toàn** — single-consumer, N agent gọi đồng thời vẫn xếp hàng đúng, `queue_ahead` minh bạch.
- **Job persistence** — state ghi xuống `~/.cici/jobs.json`; restart server giữa job thì job in-flight bị đánh FAILED (kèm lỗi rõ ràng) thay vì biến mất, job đã xong vẫn tra cứu được sau restart (retention 7 ngày).
- **Quota tracking** — đếm rolling 24h local, auto-learn threshold, hết quota thì fail nhanh (exit 4); `--wait-for-quota` tự chờ + retry khi slot roll ra window.
- **`cici doctor`** — check toàn bộ prerequisites trong 1 lệnh.
- **Self-contained package** — `pipx install` là chạy được, không cần giữ folder repo.

---

## Quickstart

```bash
# 1. Cài (một lần) — chọn 1 trong 3 cách
pipx install cici_cli                                    # từ PyPI (khi publish)
pipx install ./dist/cici_cli-0.3.0-py3-none-any.whl      # từ wheel
pipx install git+https://github.com/TongDucThanhNam/cici-api.git   # từ git

# 2. Cài app Cici (Dola Browser) + đăng nhập account ByteDance — thủ công, 1 lần
#    https://www.ciciai.com/  (login cần account/mật khẩu của bạn, tool không tự login được)

# 3. Kiểm tra môi trường rồi gen
cici doctor                              # tất cả phải ✓ (hoặc ! warn)
cici image "mèo orange dễ thương, phong cách chibi" --json
```

> 💡 **Auto-launch**: mặc định CLI tự khởi động Cici (có CDP) + core server ngầm
> nếu chưa chạy. Bạn chỉ cần mở Cici + login thủ công 1 lần. Tắt bằng
> `--no-auto-launch` (chỉ check + hướng dẫn).

---

## Cài đặt

### Yêu cầu

| | |
|---|---|
| Python | ≥ 3.10 (đã test 3.14) |
| App Cici | [Dola Browser / Cici](https://www.ciciai.com/) đã cài + đăng nhập |
| OS | Windows: đầy đủ tính năng (auto-launch Cici + server). macOS/Linux: tự chạy server được, còn Cici phải mở thủ công với `--remote-debugging-port=9222` |

### pipx (khuyến nghị — giống npx: 1 lệnh, env isolate)

```bash
pipx install cici_cli          # PyPI (khi publish)
pipx install ./dist/cici_cli-0.3.0-py3-none-any.whl
```

Chưa có pipx? `python -m pip install --user pipx && pipx ensurepath`.

### One-liner (host wheel qua GitHub Releases)

Build wheel rồi upload vào GitHub Release của repo (đặt tên asset
`cici_cli-latest-py3-none-any.whl` để URL mặc định không cần đổi mỗi phiên bản):

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/TongDucThanhNam/cici-api/master/install-web.ps1 | iex
```
```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/TongDucThanhNam/cici-api/master/install-web.sh | sh
```

Script mẫu: `install-web.ps1` / `install-web.sh` trong repo — mặc định tải wheel
từ `https://github.com/TongDucThanhNam/cici-api/releases/latest/download/`,
override bằng env `CICI_WHEEL_URL` nếu host ở chỗ khác (S3, server riêng, ...).

### pip thường

```bash
pip install ./dist/cici_cli-0.3.0-py3-none-any.whl
```

Từ **v0.3.0** wheel là self-contained: CLI + core server + config mặc định đều
đóng gói sẵn. Server tự spawn ngầm (`python -m cici.server`) khi gen lần đầu và
tự copy config ra `~/.cici/config.yaml` để bạn chỉnh.

### Dev (chạy từ repo)

```bash
git clone <repo> && cd cici-api
pip install -r requirements.txt
pip install -e .          # lệnh `cici` chạy code trong repo
```

Trong repo, `main.py` / `cici_driver.py` là shim backward-compat — code thật nằm
ở `cici/server.py` / `cici/driver.py`. Chạy server tay:
`python -m cici.server` (hoặc `uvicorn cici.server:app --port 8000`).

---

## CLI (`cici`)

### Tổng quan lệnh

| Lệnh | Chức năng |
|---|---|
| `cici image <prompt>` | Gen ảnh (block tới xong, ~30s–3 phút tuỳ model) |
| `cici video <prompt>` | Gen video (block tới xong) |
| `cici doctor` | Check prerequisites: Python, deps, config, app Cici, CDP, login, server, quota |
| `cici health` | Check nhanh server + CDP (không auto-launch) |
| `cici status <job_id>` | Xem trạng thái 1 job (poll 1 lần, không block) |
| `cici quota` | Quota còn lại (rolling 24h + threshold đã học) |
| `cici models` | Model + generation options khả dụng |

Tất cả đều có `--json`. Option chung: `--base <url>` (core server, mặc định
`http://127.0.0.1:8000` hoặc env `CICI_API`), `--no-auto-launch`.

### Gen ảnh

```bash
cici image "mèo orange dễ thương, phong cách chibi"
cici image "..." -m seedream-4.5              # chọn model
cici image "..." --ratio 16:9                 # tỷ lệ khung hình
cici image "..." --style watercolor           # phong cách
cici image "..." --ref a.png --ref b.png      # ảnh tham chiếu (lặp lại, hoặc phẩy: --ref a.png,b.png; tối đa 10)
cici image "..." --wait-for-quota             # hết quota → tự chờ + retry (xem phần Quota tracking)
cici image "..." --json                       # stdout JSON cho agent
```

### Gen video

```bash
cici video "thuyền buồm trên biển lúc hoàng hôn"
cici video "..." -m seedance-2-fast           # model nhanh
cici video "..." --ratio 9:16
cici video "..." --duration 5s                # 5s / 10s
cici video "..." --ref a.png                  # image-to-video: ảnh = frame đầu
cici video "..." --wait-for-quota             # hết quota → tự chờ + retry (xem phần Quota tracking)
```

### Models

| Loại | Alias (`--model`) | Tên | Ghi chú |
|---|---|---|---|
| image | `seedream-5-pro` *(default)* | Seedream 5.0 Pro | chất lượng cao nhất |
| image | `seedream-4.5` | Seedream 4.5 | nhanh hơn (~40s vs ~140s) |
| video | `seedance-2.5` *(default)* | Dreamina Seedance 2.5 | chất lượng tốt nhất |
| video | `seedance-2-fast` | Dreamina Seedance 2.0 Nhanh | nhanh |
| video | `seedance-1.0` | Dreamina Seedance 1.0 | đơn giản |

**Generation options** (đầy đủ trong `cici models` / `config.yaml`):

| Loại | Flag | Giá trị |
|---|---|---|
| image | `--ratio` | `1:1, 2:3, 3:4, 4:3, 9:16, 16:9` |
| image | `--style` | `portrait, landscape, anime, 3d, cyberpunk, oil-painting, watercolor, flat-illustration, children-drawing, pixel, colored-pencil, ink-wash, ink` |
| video | `--ratio` | `1:1, 3:4, 4:3, 9:16, 16:9, 21:9` |
| video | `--duration` | `5s, 10s` |

Khi Cici thêm model/option mới → chạy `python inspect_skills.py` re-check rồi
cập nhật `config.yaml` (`models:` + `options:`).

### Quota tracking

Cici free có giới hạn gen hằng ngày nhưng **không tiết lộ** số còn lại / khi
reset. Tool tự track local:

- **`cici quota`** — đã dùng (rolling 24h), threshold đã học, còn lại, khi reset.
- **Auto-learn threshold** — hit limit lần đầu → tool ghi nhớ "N gen thì hết" → từ đó **từ chối gen trước khi tốn thời gian chờ fail** (exit 4).
- **Rolling 24h** — gen cũ tự drop khỏi count sau 24h, phản ánh cách ByteDance limit.
- **`--wait-for-quota`** — hết quota (429 lúc enqueue hoặc bị chặn giữa job) thì
  thay vì exit 4 ngay, CLI tự chờ tới khi slot cũ nhất roll ra rolling window
  (daily cap) hoặc vài phút (rate-limit burst) rồi re-enqueue cùng prompt.
  Giới hạn bởi `--quota-max-wait <giây>` (mặc định `quota.max_wait_seconds` = 6h)
  và `quota.max_attempts` (3 lần) trong `config.yaml` — vượt giới hạn thì vẫn
  exit 4 như cũ, không treo. Không có `--wait-for-quota` thì `--quota-max-wait`
  bị bỏ qua.
- **`--account <nhãn>`** — tách quota local theo nhãn account: bạn TỰ đổi account
  trong app Cici (logout/login thủ công), rồi gán nhãn khi gen để tool đếm
  rolling 24h + threshold riêng từng account (state ở `~/.cici/quota-<nhãn>.json`,
  xem bằng `cici quota --account <nhãn>`). Tool KHÔNG tự đổi account, không đụng
  login/session — nhãn chỉ là bookkeeping, không tăng quota.

State ở `~/.cici/quota.json` (hoặc `~/.cici/quota-<nhãn>.json` khi dùng
`--account`) — xoá file để reset. Quota là **local estimate**
(đọc file hỏng thì fail-open, không chặn gen).

### Exit codes

| Code | Ý nghĩa | Khi nào |
|---|---|---|
| `0` | COMPLETED | gen xong, có URLs |
| `1` | FAILED | job lỗi server-side (kể cả `CONTENT_BLOCKED` — xem [Giới hạn](#giới-hạn-đã-biết)) |
| `2` | TIMEOUT | gen không xong trong timeout (300s ảnh / 600s video — theo `config.yaml`, server trả kèm `timeout_s` trong 202) — **chỉ tính thời gian PROCESSING**; chờ hàng đợi (PENDING) tính riêng theo `queue_ahead` |
| `3` | PREFLIGHT | server/Cici chưa chạy, **hoặc mất kết nối server giữa chừng** (`POLL_ERROR` — job có thể vẫn đang chạy, kiểm tra `cici status <job_id>`) |
| `4` | QUOTA_EXHAUSTED | hết quota hằng ngày — **đừng retry ngay**, chờ reset (rolling 24h). Trừ khi chạy với `--wait-for-quota`: CLI tự chờ + retry trong giới hạn rồi mới exit 4 |

### Dành cho AI coding agent

- `cici image` **block ~2–4 phút** → set tool timeout cao (≥ 320s).
- `--json`: kết quả cuối in ra **stdout** (1 JSON duy nhất), progress log ra stderr → đọc pipe stdout là parse được.
- URL kết quả có `x-expires` (parse sẵn field `expires_local`) — thường xa ~10 năm nên hiếm khi gấp.
- **Chất lượng ảnh**: driver tự nâng URL từ preview (~288px, watermark lớn) lên **ảnh gốc full-size** (vd 1773×2364) bằng image viewer (template `image_pre_watermark`). Nếu viewer đổi/fail → job vẫn COMPLETED với URL preview (fallback), log ghi `Full-size upgrade: N/M`. Ảnh gốc vẫn còn watermark nhỏ "AI generated" ở góc — do Cici áp lên chính file gốc, không có bản sạch qua UI này.
- Nhiều agent gọi đồng thời: queue xử lý tuần tự, `PENDING` kéo dài là bình thường (xem `queue_ahead` trong `cici status`).

---

## HTTP API

Core server là FastAPI local (bind `127.0.0.1:8000`, tự spawn khi cần).

| Method | Path | Mô tả |
|---|---|---|
| `GET` | `/api/health` | Cici CDP reachable? + queue size |
| `POST` | `/api/generate` | Enqueue job → `202` + `job_id` + `timeout_s` |
| `GET` | `/api/status/{job_id}` | Trạng thái + kết quả + `queue_ahead` + `queue_size` |
| `GET` | `/api/models` | Model registry + generation options |
| `GET` | `/api/quota` | Quota snapshot (`?kind=image\|video`, `?account=<nhãn>`) |
| `GET` | `/api/jobs` | Job gần đây (debug) |

**Gen:**

```bash
curl -X POST http://127.0.0.1:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt":"một con mèo orange dễ thương","type":"image"}'
# → 202 {"job_id":"…","status":"PENDING","timeout_s":300}
```

Payload đầy đủ (ngoài `prompt` đều optional):

```json
{
  "prompt": "…",
  "type": "image | video",
  "model": "seedream-4.5",
  "references": ["C:/path/a.png", "C:/path/b.png"],
  "ratio": "16:9",
  "style": "watercolor",
  "duration": "5s",
  "account": "nhãn-tuỳ-chọn"
}
```

- `references` — đường dẫn local, tối đa 10, hỗ trợ cả **video** (image-to-video).
- `style` chỉ dùng cho image; `duration` chỉ cho video.
- `account` — nhãn tuỳ chọn để tách quota local theo account (bạn tự đổi account
  trong app Cici; tool không tự đổi). Xem phần Quota tracking.
- Giá trị không hợp lệ → `422` kèm danh sách hợp lệ.

**Poll:**

```bash
curl http://127.0.0.1:8000/api/status/<job_id>
```

Trạng thái: `PENDING → PROCESSING → COMPLETED` (kèm `result_urls[]`), `FAILED`,
`QUOTA_EXHAUSTED`, hoặc `CONTENT_BLOCKED`.

**Smoke test tự động** (gen 1 ảnh thật, tốn quota): `python test_e2e.py`.

---

## Kiến trúc

```
Client  ──POST /api/generate──▶  FastAPI  ──▶  asyncio.Queue  ──▶  Worker (1 luồng)
        ◀──202 + job_id + timeout_s──┘            │                    │
                                                  │                    ▼
Client  ──GET /api/status/{id}──▶  JobStore  ◀── update ──  Playwright (CDP → Cici)
```

- **Producer/Consumer + async polling** — gen mất 2–5 phút, không block HTTP. N request → xếp hàng tuần tự (Cici chỉ có 1 ô chat; worker đơn luồng là bất biến).
- **Race-safe theo snapshot** — driver chụp số bot messages + set media URL trước khi gửi; chỉ tin kết quả xuất hiện sau đó. Kết quả job trước không thể leak vào job sau.
- **Hai tầng deadline** — gen timeout (`timing.<kind>_timeout`) trong polling + hard deadline (thêm `timing.hard_deadline_margin`) bằng `asyncio.wait_for`: driver treo thì job `FAILED`, queue đi tiếp.
- **CDP loss có bound** — `_attach()` bỏ cuộc sau `cdp.connect_timeout` (90s) với lỗi rõ ràng thay vì treo vĩnh viễn.
- **Job persistence** — mọi update job được ghi xuống `~/.cici/jobs.json` (best-effort, file hỏng thì fail-open). Restart server: job đã xong được restore, job đang chờ/dang xử lý bị đánh FAILED với lỗi "server restarted mid-job" — agent retry là xong. Job terminal cũ hơn 7 ngày tự bị prune khỏi file. Queue vẫn in-memory (job chưa xử lý không sống qua restart).
- **Self-contained package** — server (`cici/server.py`) spawn bằng `python -m cici.server`, không phụ thuộc folder repo.

---

## Cấu hình

Toàn bộ selector + model registry + timing nằm trong **`config.yaml`**. Khi Cici
đổi UI (đổi class/testid), **chỉ sửa file này** — không đụng code.

Thứ tự resolve (xem `cici/_config.py`):

1. env `CICI_CONFIG=<đường dẫn>`
2. `./config.yaml` trong CWD (dev — chạy từ repo)
3. `~/.cici/config.yaml` — bản user-editable (server tự copy từ default lần đầu)
4. config đóng gói trong package (fallback)

Trích yếu quan trọng:

```yaml
selectors:
  creation_tab_image: '[data-testid="creation-skill-switch-tab-image"]'   # build 147.0.7727.149+
  model_button: 'button:has-text("Model")'
  ratio_button: 'button:has-text("Tỷ lệ")'
  style_button: 'button:has-text("Phong cách")'        # image only
  duration_button: 'button:has-text("5s"), button:has-text("10s")'   # video only
  ref_button: '[data-testid="image-creation-chat-input-picture-reference-button"]'
  send_button: '[data-testid="chat_input_send_button"]'
  done_indicator: '[data-testid="message_action_bar"]'  # xuất hiện = gen xong
  result_image: '[data-testid="mdbox_image"] img'
  result_video: 'div[class*="block-video"]'   # video block (click lazy-init <video>)
  fullsize_image_marker: "image_pre_watermark"  # template ảnh gốc trong viewer

timing:
  image_timeout: 300        # giây
  video_timeout: 600
  hard_deadline_margin: 180 # + timeout = hard deadline mỗi job
```

**Environment variables:**

| Biến | Ý nghĩa |
|---|---|
| `CICI_API` | Base URL core server (mặc định `http://127.0.0.1:8000`) |
| `CICI_CONFIG` | Đường dẫn config.yaml override |
| `CICI_EXE` | Đường dẫn Cici.exe (nếu cài chỗ khác mặc định) |
| `CICI_USER_DATA` | User-data dir của Cici |

---

## Xử lý sự cố

| Triệu chứng | Nguyên nhân & cách xử lý |
|---|---|
| `cici not recognized` | pipx/pip Scripts dir chưa có trong PATH. pipx: chạy `pipx ensurepath` rồi mở terminal mới. pip: thêm `%APPDATA%\Python\Python314\Scripts` vào PATH. |
| `cici doctor` → `cici-cdp` ✗/! | Cici chưa chạy với CDP. Windows: `start_cici.bat` (tự relaunch có CDP) hoặc để CLI tự launch khi gen. |
| `cici doctor` → `cici-login` ✗ | Mở cửa sổ Cici, đăng nhập account ByteDance, chạy lại doctor. |
| Job treo / queue không nhúc nhích | Xem log server `~/.cici/server.log`. Hard deadline sẽ tự đánh FAILED job treo (mặc định timeout + 180s) — nếu lặp lại, restart Cici (`start_cici.bat`). |
| Server restart giữa job | Job đang chờ/xử lý bị đánh FAILED (lỗi "server restarted mid-job") khi server lên lại — enqueue lại là xong. Job đã xong vẫn tra cứu được bằng `cici status` sau restart (lưu 7 ngày trong `~/.cici/jobs.json`). |
| Đã sửa code/config nhưng hành vi cũ | Lệnh `cici` luôn dùng code mới; **server chạy ngầm giữ code cũ** — kill rồi cho CLI spawn lại: `taskkill /F /PID <pid của cổng 8000>` (tìm bằng `netstat -ano | findstr :8000`). |
| Cici đổi UI → job FAILED liên tục | Chạy `python inspect_dom.py` / `python inspect_skills.py`, sửa selector trong `~/.cici/config.yaml`. |
| `429` / exit 4 liên tục | Hết quota rolling 24h — xem `cici quota`, chờ reset. Muốn reset thủ công: xoá `~/.cici/quota.json`. |

---

## Giới hạn đã biết

- **Content block / bản quyền** — Cici có thể ĐÃ gen xong nhưng **từ chối hiển thị kết quả** (vd "...vì âm thanh trong video..."). Driver detect refusal → trạng thái `CONTENT_BLOCKED` (exit 1), fail nhanh thay vì spin tới timeout. Đây là filter của Cici (không phải lỗi tool) → đổi ảnh tham chiếu / sửa prompt rồi retry (vd thêm `no sound` / `silent` cho video). Patterns ở `config.yaml` → `messages.refusal_patterns`.
- **Watermark "AI generated"** — Cici áp lên chính file gốc; không có bản no-watermark qua UI này.
- **Model/ratio/style text là localized** — `select_text` theo ngôn ngữ UI Cici; đổi ngôn ngữ = cập nhật config.
- **Tốc độ** — tuần tự ~2 phút/job ảnh; không phù hợp throughput cao.
- **Quota** — dùng quota free của account Cici; rate-limit / block có thể xảy ra bất cứ lúc nào.
- **UI Cici cập nhật** = phải re-inspect config (xem [Xử lý sự cố](#xử-lý-sự-cố)).

---

## Phát triển

```bash
pip install -r requirements.txt && pip install -e .
python -m cici.server                       # chạy server từ repo
```

Tests (xuất phát từ repo root, không tốn quota):

```bash
python -m compileall -q main.py cici_driver.py cici tests   # syntax
python tests/test_wait_status.py            # queue-aware client polling
python tests/test_result_detection.py       # result-polling JS trên fixture DOM
python tests/test_quota.py                  # quota + --wait-for-quota loop (mock)
python tests/test_accounts.py               # quota theo nhãn account + CLI flag
python tests/test_persist.py                # job persistence + retention
python tests/stress_test.py                 # server thật + fake driver: 44 checks
python test_e2e.py                          # LIVE smoke (tốn quota — chỉ khi cần)
```

### File map

```
cici-api/
├── cici/                      # package (wheel tự chứa: CLI + server + config)
│   ├── cli.py                 # Click commands: image/video/doctor/health/status/quota/models
│   ├── server.py              # FastAPI endpoints + queue/store + lifespan (shim: main.py)
│   ├── driver.py              # Playwright CDP driver + worker loop (shim: cici_driver.py)
│   ├── _config.py             # config resolution (env > cwd > ~/.cici > packaged)
│   ├── _client.py             # sync HTTP client + wait_status + URL expiry parser
│   ├── _launcher.py           # auto-launch Cici + spawn server (python -m cici.server)
│   ├── _quota.py              # rolling 24h quota tracker + auto-learn threshold
│   ├── _persist.py            # job persistence (~/.cici/jobs.json) + boot reconcile + retention
│   └── config.yaml            # config mặc định đóng gói (giữ đồng bộ với bản repo-root)
├── main.py / cici_driver.py   # shim backward-compat (re-export từ cici/)
├── config.yaml                # config nguồn (dev) — giữ đồng bộ với cici/config.yaml
├── tests/
│   ├── test_result_detection.py   # fixture-DOM tests cho poll JS
│   ├── test_wait_status.py        # queue-aware wait tests
│   ├── test_quota.py              # quota + --wait-for-quota tests
│   ├── test_accounts.py           # quota theo nhãn account tests
│   ├── test_persist.py            # persistence + retention tests
│   ├── stress_test.py             # stress suite (server thật + fake driver)
│   └── _cli_entry.py              # entry cho stress chạy CLI trong process thật
├── test_e2e.py                # LIVE smoke (tốn quota)
├── inspect_dom.py             # (dev) read-only DOM probe khi UI đổi
├── inspect_result_images.py   # (dev) probe result-image DOM (preview vs full-size)
├── inspect_skills.py          # (dev) probe skill/create-image DOM (không gen)
├── start_cici.bat             # launcher Cici có CDP (thủ công)
├── install.ps1 / install.sh   # installer repo-local (dev)
├── install-web.ps1 / install-web.sh   # one-liner installer để host (set CICI_WHEEL_URL)
├── pyproject.toml / requirements.txt
└── LICENSE                    # MIT
```

---

## License

MIT — xem [LICENSE](LICENSE).

<!-- MARKDOWN LINKS & BADGES -->
[stars-shield]: https://img.shields.io/github/stars/TongDucThanhNam/cici-api?style=for-the-badge
[stars-url]: https://github.com/TongDucThanhNam/cici-api/stargazers
[forks-shield]: https://img.shields.io/github/forks/TongDucThanhNam/cici-api?style=for-the-badge
[forks-url]: https://github.com/TongDucThanhNam/cici-api/network/members
[issues-shield]: https://img.shields.io/github/issues/TongDucThanhNam/cici-api?style=for-the-badge
[issues-url]: https://github.com/TongDucThanhNam/cici-api/issues
[license-shield]: https://img.shields.io/github/license/TongDucThanhNam/cici-api?style=for-the-badge
[license-url]: https://github.com/TongDucThanhNam/cici-api/blob/master/LICENSE
[python-shield]: https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white
[python-url]: https://www.python.org/
[fastapi-shield]: https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white
[fastapi-url]: https://fastapi.tiangolo.com/
[playwright-shield]: https://img.shields.io/badge/Playwright-2EAD33?style=for-the-badge&logo=playwright&logoColor=white
[playwright-url]: https://playwright.dev/
[windows-shield]: https://img.shields.io/badge/Windows%20auto--launch-0078D6?style=for-the-badge&logo=windows&logoColor=white
[windows-url]: #cài-đặt
