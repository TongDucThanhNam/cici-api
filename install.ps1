# Cici API — one-click installer (Windows / PowerShell)
# Cài: deps Python, cici-cli package, thêm cici.exe vào PATH.
# Chạy:  powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
Write-Host "=== Cici API installer ===" -ForegroundColor Cyan

# 1. Check Python
$py = (python --version 2>$null)
if ($LASTEXITCODE -ne 0) {
    Write-Host "[X] Python chưa cài. Cài Python 3.10+ từ https://python.org rồi chạy lại." -ForegroundColor Red
    exit 1
}
Write-Host "[OK] $py"

# 2. Cài deps core + CLI
Write-Host "`n[1/3] Cài Python dependencies..." -ForegroundColor Yellow
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "[X] Lỗi cài requirements.txt" -ForegroundColor Red; exit 1 }
Write-Host "[OK] dependencies installed"

# 3. Cài cici-cli package (editable)
Write-Host "`n[2/3] Cài cici-cli package..." -ForegroundColor Yellow
python -m pip install -e . --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "[X] Lỗi cài package" -ForegroundColor Red; exit 1 }
Write-Host "[OK] cici-cli installed"

# 4. Thêm Scripts dir vào PATH (persistent, 1 lần)
Write-Host "`n[3/3] Thêm cici vào PATH..." -ForegroundColor Yellow
$scriptsDir = "$env:APPDATA\Python\Python314\Scripts"
# detect scripts dir (could be Python3xx khác)
$cand = Get-ChildItem "$env:APPDATA\Python" -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
if ($cand) { $scriptsDir = "$($cand.FullName)\Scripts" }

if (Test-Path "$scriptsDir\cici.exe") {
    $userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
    if ($userPath -notlike "*$scriptsDir*") {
        [Environment]::SetEnvironmentVariable('PATH', "$userPath;$scriptsDir", 'User')
        Write-Host "[OK] Đã thêm $scriptsDir vào PATH (user). Mở terminal MỚI để dùng lệnh 'cici'." -ForegroundColor Green
    } else {
        Write-Host "[OK] $scriptsDir đã có trong PATH." -ForegroundColor Green
    }
} else {
    Write-Host "[!] Không tìm thấy cici.exe trong $scriptsDir — gọi bằng 'python -m cici.cli' thay thế." -ForegroundColor Yellow
}

Write-Host "`n=== Hoàn tất ===" -ForegroundColor Cyan
Write-Host "Bước tiếp theo (xem README.md):" -ForegroundColor White
Write-Host "  1. Chạy start_cici.bat  (khởi động Cici có CDP)"
Write-Host "  2. Server tự khởi động khi gen lần đầu (muốn chạy tay: python -m cici.server)"
Write-Host "  3. Mở terminal MỚI rồi: cici health  (test)"
Write-Host ""
