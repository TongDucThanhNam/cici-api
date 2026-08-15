#!/usr/bin/env sh
# One-line installer cho cici-cli (macOS/Linux) — kiểu Codex CLI.
#
# Triển khai (GitHub Releases): build wheel (`python -m pip wheel . --no-deps -w dist`),
# rename thành cici_cli-latest-py3-none-any.whl, upload vào Release của repo:
#   https://github.com/TongDucThanhNam/cici-api/releases
# Rồi khách chạy:
#   curl -fsSL https://raw.githubusercontent.com/TongDucThanhNam/cici-api/master/install-web.sh | sh
#
# Host chỗ khác: set env CICI_WHEEL_URL (hoặc sửa dòng mặc định dưới đây).
set -e

CICI_WHEEL_URL="${CICI_WHEEL_URL:-https://github.com/TongDucThanhNam/cici-api/releases/latest/download/cici_cli-latest-py3-none-any.whl}"

echo "cici-cli installer (macOS/Linux)"

# 1. Python >= 3.10
if ! command -v python3 >/dev/null 2>&1; then
    echo "[FAIL] Khong tim thay python3. Cai Python >= 3.10 roi chay lai." >&2
    exit 1
fi
echo "[OK] $(python3 --version)"

# 2. pipx — isolate env kiểu npx; fallback pipx ensurepath
python3 -m pip install --user pipx >/dev/null 2>&1 || true
if command -v pipx >/dev/null 2>&1; then
    PIPX=pipx
else
    PIPX="python3 -m pipx"
fi

# 3. cai wheel
$PIPX install "$CICI_WHEEL_URL" || $PIPX install --force "$CICI_WHEEL_URL"
$PIPX ensurepath >/dev/null 2>&1 || true

# 4. verify
echo ""
echo "Kiem tra cai dat:"
cici --version || ~/.local/bin/cici --version
echo ""
echo "Buoc tiep theo:"
echo "  1. Cai + dang nhap app Cici (Dola Browser) neu chua co."
echo "  2. Chay Cici voi CDP: Cici --remote-debugging-port=9222 (macOS/Linux tu launch)."
echo "  3. Chay: cici doctor"
echo "  4. Gen:   cici image 'mot con meo orange' --json"
