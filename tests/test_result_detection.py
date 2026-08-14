"""Deterministic tests for cici_driver result-detection logic (không cần Cici).

Chạy chính xác _POLL_RESULT_JS / _SNAPSHOT_JS trong cici_driver lên fixture
DOM qua Playwright chromium local. Không tốn quota, không đụng session.

    python tests/test_result_detection.py

Nếu chromium chưa cài (`playwright install chromium`), skip với thông báo rõ.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cici_driver import _POLL_RESULT_JS, _SNAPSHOT_JS, CiciDriver, load_config  # noqa: E402
from cici import _quota  # noqa: E402

SEL = {
    "message_list": '[data-testid="message-list"]',
    "bot_message": '[data-testid="receive_message"]',
    "done_indicator": '[data-testid="message_action_bar"]',
    "result_image": '[data-testid="mdbox_image"] img',
    "result_video": 'div[class*="block-video"]',
}

IMG1 = "https://cdn.example.com/rc_gen_image/a1.jpeg"
IMG2 = "https://cdn.example.com/rc_gen_image/a2.jpeg"
IMG_OLD = "https://cdn.example.com/rc_gen_image/old.jpeg"
VIDEO1 = "https://v16-dola.dola.com/abc/video/tos/mya/v1.mp4"

# Fixture HTML: message-list với N bot message cũ + 1 bot message mới.
CHAT_PAGE = """
<div data-testid="message-list">
  {old}
  {new}
</div>
"""


def bot_msg(imgs=(), videos=(), done=True, text=""):
    media = ""
    for s in imgs:
        media += f'<div data-testid="mdbox_image"><img src="{s}"></div>'
    for v in videos:
        media += f'<div class="block-video-X"><img class="cover-x" src="{v}_cover.png"></div>'
    action = '<div data-testid="message_action_bar"></div>' if done else ""
    return (
        f'<div data-testid="receive_message">{text}{media}{action}</div>'
    )


def run_poll(page, before=0, media_before=None):
    return page.evaluate(
        _POLL_RESULT_JS,
        {"sel": SEL, "before": before, "mediaBefore": media_before or []},
    )


def run_snapshot(page):
    return page.evaluate(_SNAPSHOT_JS, {"sel": SEL})


def main() -> int:
    from playwright.sync_api import sync_playwright

    passed = 0
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as e:  # noqa: BLE001
        print(f"SKIP: không khởi động được Playwright chromium ({e}).")
        print("      Chạy `playwright install chromium` rồi thử lại.")
        return 0  # skip có lý do rõ ràng, không coi là fail

    page = browser.new_page()
    try:
        # 1. images-only: chỉ thu URLs từ bot message MỚI (isolation theo `before`)
        html = CHAT_PAGE.format(
            old=bot_msg(imgs=[IMG_OLD], done=True, text="old job"),
            new=bot_msg(imgs=[IMG1, IMG2], done=True, text="new job"),
        )
        page.set_content(html)
        res = run_poll(page, before=1)
        assert res["mode"] == "chat", res
        assert res["newRecv"] == 1, res
        assert res["done"] is True, res
        assert res["urls"] == [IMG1, IMG2], res
        assert IMG_OLD not in res["urls"], res
        passed += 1
        print("PASS 1: chat branch, image URLs + before-isolation")

        # 2. video blocks: poll 1 click block (chưa có <video>), poll 2 đọc src
        html = CHAT_PAGE.format(
            old="",
            new=bot_msg(videos=[VIDEO1], done=True, text="Video đã tạo"),
        )
        page.set_content(html)
        res1 = run_poll(page, before=0)
        assert res1["videoBlocks"] == 1, res1
        assert res1["urls"] == [], res1  # chưa có <video>, mới chỉ click
        # giả lập xgplayer đã init: inject <video> vào block
        page.evaluate(
            """(src) => {
                const b = document.querySelector('div[class*="block-video"]');
                const v = document.createElement('video');
                v.src = src;
                b.appendChild(v);
            }""",
            VIDEO1,
        )
        res2 = run_poll(page, before=0)
        assert res2["done"] is True, res2
        assert res2["urls"] == [VIDEO1], res2
        passed += 1
        print("PASS 2: video block lazy-init click + src extraction")

        # 3. done nhưng chưa có media → không coi là thành công (urls rỗng)
        html = CHAT_PAGE.format(
            old="",
            new=bot_msg(imgs=[], done=True, text="done but empty"),
        )
        page.set_content(html)
        res = run_poll(page, before=0)
        assert res["done"] is True and res["urls"] == [], res
        passed += 1
        print("PASS 3: done-without-media returns empty urls (keep polling)")

        # 4. quota-exhausted text có mặt trong bot message mới
        html = CHAT_PAGE.format(
            old="",
            new=bot_msg(imgs=[], done=False, text="Bạn đã đạt đến giới hạn tạo hình ảnh"),
        )
        page.set_content(html)
        res = run_poll(page, before=0)
        assert _quota.is_exhausted_message(res["text"]), res
        passed += 1
        print("PASS 4: quota-exhausted text detected from bot message")

        # 5. chưa có bot message mới → newRecv=0 (job chưa được nhận)
        html = CHAT_PAGE.format(old=bot_msg(imgs=[IMG_OLD], done=True), new="")
        page.set_content(html)
        res = run_poll(page, before=1)
        assert res["newRecv"] == 0 and res["urls"] == [], res
        passed += 1
        print("PASS 5: no new bot message yet -> newRecv=0")

        # 6. video nằm trong bot message TRƯỚC message cuối (done ở message cuối)
        html = CHAT_PAGE.format(
            old="",
            new=(
                bot_msg(videos=[VIDEO1], done=True, text="Video đã tạo: ...")
                + bot_msg(imgs=[], done=True, text="Đã tạo xong video cho bạn.")
            ),
        )
        page.set_content(html)
        page.evaluate(
            """(src) => {
                const b = document.querySelector('div[class*="block-video"]');
                const v = document.createElement('video');
                v.src = src;
                b.appendChild(v);
            }""",
            VIDEO1,
        )
        res = run_poll(page, before=0)
        assert res["done"] is True, res
        assert res["urls"] == [VIDEO1], res
        passed += 1
        print("PASS 6: video collected across all NEW bot messages")

        # 7. filter data:/blob: URLs
        html = CHAT_PAGE.format(
            old="",
            new=bot_msg(imgs=["data:image/png;base64,AAAA", IMG1], done=True),
        )
        page.set_content(html)
        res = run_poll(page, before=0)
        assert res["urls"] == [IMG1], res
        passed += 1
        print("PASS 7: data: URLs filtered out")

        # 8. inline branch: không có message-list → diff với snapshot trước send
        page.set_content(
            '<div data-testid="mdbox_image"><img src="%s"></div>' % IMG1
        )
        snap = run_snapshot(page)
        assert snap == {"before": 0, "media": [IMG1]}, snap
        res = run_poll(page, before=0, media_before=snap["media"])
        assert res["mode"] == "inline", res
        assert res["urls"] == [], res  # chưa có media mới
        page.evaluate(
            """(src) => {
                const d = document.createElement('div');
                d.setAttribute('data-testid', 'mdbox_image');
                d.innerHTML = '<img src="' + src + '">';
                document.body.appendChild(d);
            }""",
            IMG2,
        )
        res2 = run_poll(page, before=0, media_before=snap["media"])
        assert res2["mode"] == "inline", res2
        assert res2["urls"] == [IMG2], res2  # chỉ media MỚI
        passed += 1
        print("PASS 8: inline branch snapshot-diff (only new media)")

        # 9. snapshot trên trang chat: đếm bot messages + media hiện có
        html = CHAT_PAGE.format(
            old=bot_msg(imgs=[IMG_OLD], done=True),
            new="",
        )
        page.set_content(html)
        snap = run_snapshot(page)
        assert snap["before"] == 1, snap
        assert snap["media"] == [IMG_OLD], snap
        passed += 1
        print("PASS 9: pre-send snapshot (bot count + media set)")

        # 10. content-block / copyright refusal text — detected from bot message
        # (Cici gen xong nhưng từ chối hiển thị → fail nhanh, không spin tới timeout)
        refusal = ("Để bảo vệ bản quyền, tôi không thể hiển thị cho bạn video "
                   "đã tạo vì âm thanh trong video. Hãy sử dụng nội dung tham chiếu "
                   "khác hoặc chỉnh sửa câu lệnh và thử lại.")
        html = CHAT_PAGE.format(
            old="",
            new=bot_msg(imgs=[], done=False, text=refusal),
        )
        page.set_content(html)
        res = run_poll(page, before=0)
        assert res["newRecv"] == 1, res
        assert res["text"] and "bản quyền" in res["text"].lower(), res
        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        driver = CiciDriver(load_config(str(cfg_path)))
        assert driver._is_refusal_message(res["text"]) is True, res["text"]
        # negative: text success bình thường KHÔNG bị flag là refusal
        assert driver._is_refusal_message("Video đã tạo xong cho bạn.") is False
        passed += 1
        print("PASS 10: copyright/content refusal detected (no timeout spin)")
    finally:
        browser.close()
        pw.stop()

    print(f"\n{passed}/10 tests passed")
    return 0 if passed == 10 else 1


if __name__ == "__main__":
    sys.exit(main())
