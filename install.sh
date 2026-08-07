#!/usr/bin/env bash
# Cici API — one-click installer (macOS / Linux / Git Bash)
# Cài: deps Python + cici-cli package. PATH tự có với pip --user.
# Chạy:  bash install.sh
set -e
echo "=== Cici API installer ==="

# 1. Python
if ! command -v python3 &>/dev/null; then
    echo "[X] python3 chưa cài. Cài Python 3.10+ rồi chạy lại."
    exit 1
fi
echo "[OK] $(python3 --version)"

# 2. Deps
echo ""
echo "[1/2] Cài Python dependencies..."
python3 -m pip install --user --upgrade pip --quiet
python3 -m pip install --user -r requirements.txt --quiet
echo "[OK] dependencies installed"

# 3. Package
echo ""
echo "[2/2] Cài cici-cli package..."
python3 -m pip install --user -e . --quiet
echo "[OK] cici-cli installed"

# 4. PATH hint
PYUSERBASE=$(python3 -m site --user-base 2>/dev/null)
echo ""
echo "=== Hoàn tất ==="
echo "Nếu 'cici' không nhận, đảm bảo \$PYUSERBASE/bin trong PATH:"
echo "  export PATH=\"$PYUSERBASE/bin:\$PATH\"   # thêm vào ~/.bashrc / ~/.zshrc"
echo ""
echo "Bước tiếp theo (xem README.md):"
echo "  1. start_cici.bat (Windows) — hoặc khởi động Cici có CDP thủ công:"
echo "     Cici --remote-debugging-port=9222 --user-data-dir=<UserData>"
echo "  2. uvicorn main:app --port 8000"
echo "  3. cici health"
