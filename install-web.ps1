# One-line installer cho cici-cli (Windows) — kiểu Codex CLI.
#
# Triển khai: build wheel (`python -m pip wheel . --no-deps -w dist`), host file
# wheel + script này ở URL công cộng (hoặc private có token), rồi khách chạy:
#   irm https://<your-host>/cici/install.ps1 | iex
#
# Lưu ý: repo TongDucThanhNam/cici-api đang PRIVATE — raw GitHub URLs / release
# assets 404 với người ngoài. Nếu publish repo thì có thể trỏ CICI_WHEEL_URL về
# https://github.com/TongDucThanhNam/cici-api/releases/latest/download/<wheel>.
#
# Trước khi host: đặt biến CICI_WHEEL_URL dưới đây trỏ tới URL wheel thật,
# hoặc để khách override qua env cùng tên.
param()

$ErrorActionPreference = "Stop"

# URL wheel mặc định — THAY bằng URL bạn host (GitHub Release asset public, S3, ...)
$env:CICI_WHEEL_URL = if ($env:CICI_WHEEL_URL) { $env:CICI_WHEEL_URL } else { "https://example.com/cici_cli-latest-py3-none-any.whl" }

Write-Host "cici-cli installer (Windows)" -ForegroundColor Cyan

# 1. Python >= 3.10
try { $py = python --version } catch { $py = $null }
if (-not $py) {
    Write-Host "[FAIL] Khong tim thay Python. Cai Python >= 3.10 tai https://python.org (tick 'Add to PATH') roi chay lai." -ForegroundColor Red
    exit 1
}
Write-Host "[OK] $py"

# 2. pipx (env isolate giong npx) — fallback pip --user
$pipxOk = $true
try { pipx --version | Out-Null } catch { $pipxOk = $false }
if (-not $pipxOk) {
    Write-Host "Dang cai pipx..."
    python -m pip install --user pipx 2>$null
    try { pipx --version | Out-Null } catch {
        python -m pipx ensurepath 2>$null
        $pipxOk = $false
    }
}

# 3. cai wheel
if ($pipxOk) {
    pipx install $env:CICI_WHEEL_URL
    if ($LASTEXITCODE -ne 0) { pipx install --force $env:CICI_WHEEL_URL }
} else {
    python -m pip install --user $env:CICI_WHEEL_URL
    Write-Host "[NOTE] Da cai bang pip --user. Kiem tra %APPDATA%\Python\Scripts co nam trong PATH."
}

# 4. verify
Write-Host ""
Write-Host "Kiem tra cai dat:" -ForegroundColor Cyan
cici --version
Write-Host ""
Write-Host "Buoc tiep theo:" -ForegroundColor Cyan
Write-Host "  1. Cai + dang nhap app Cici (Dola Browser) neu chua co."
Write-Host "  2. Chay: cici doctor   (check tat ca prerequisites)"
Write-Host "  3. Gen:   cici image `"mot con meo orange`" --json"
