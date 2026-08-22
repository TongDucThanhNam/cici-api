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

from cici_driver import _FULLSIZE_JS, _POLL_RESULT_JS, _SNAPSHOT_JS, CiciDriver, load_config  # noqa: E402
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


def run_poll(page, before=0, media_before=None, kind="image"):
    return page.evaluate(
        _POLL_RESULT_JS,
        {"sel": SEL, "before": before, "mediaBefore": media_before or [], "kind": kind},
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
        res1 = run_poll(page, before=0, kind="video")
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
        res2 = run_poll(page, before=0, kind="video")
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
        res = run_poll(page, before=0, kind="video")
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

        # 11. full-size upgrade JS: map base path -> URL gốc image_pre_watermark
        # (preview downsize_watermark ~288px; bản gốc do viewer lazy-load)
        BASE_A = IMG1.split("~tplv")[0] if "~tplv" in IMG1 else "https://cdn.example.com/rc_gen_image/a1.jpeg"
        BASE_B = "https://cdn.example.com/rc_gen_image/b1.jpeg"
        PREVIEW_A = BASE_A + "~tplv-xxx-downsize_watermark_1_5.png?sig=1"
        FULL_A = BASE_A + "~tplv-xxx-image_pre_watermark_1_5.png?sig=2"
        FULL_B = BASE_B + "~tplv-xxx-image_pre_watermark_1_5.png?sig=3"
        page.set_content(
            f'<div><img src="{PREVIEW_A}">'
            f'<img src="{FULL_A}">'
            f'<img src="data:image/png;base64,AAAA">'
            f'<img src="{FULL_B}"></div>'
        )
        got = page.evaluate(_FULLSIZE_JS, {"marker": "image_pre_watermark"})
        assert got.get(BASE_A) == FULL_A, got
        assert got.get(BASE_B) == FULL_B, got
        assert len(got) == 2, got   # data: bị bỏ, preview không chứa marker
        # Python-side matching trong _upgrade_to_fullsize: base khớp -> dùng bản gốc,
        # base thiếu (viewer chưa load) -> giữ preview
        out = [got.get(b, p) for b, p in {BASE_A: PREVIEW_A,
                                          "https://cdn.example.com/rc_gen_image/missing.jpeg": "preview-missing"}.items()]
        assert out[0] == FULL_A and out[1] == "preview-missing", out
        passed += 1
        print("PASS 11: full-size marker map (base -> image_pre_watermark URL)")

        # 12. Doubao video: KHÔNG action bar trên video message → done fallback
        # theo text (video_done_patterns) + <video> src có sẵn (hover-init).
        # Verify live 2026-08-22: Doubao video message text "你的视频生成好了".
        sel_doubao = dict(SEL, video_done_patterns=["视频生成好", "video is ready"])
        page.set_content(
            CHAT_PAGE.format(
                old=bot_msg(done=True, text="old"),
                new=(
                    '<div data-testid="receive_message">生成视频：a cat，10s\n你的视频生成好了。'
                    '<div class="block-video-Db"><div class="video-player-Db">'
                    f'<video src="{VIDEO1}"></video></div></div>'
                    "</div>"
                ),
            )
        )
        res = page.evaluate(
            _POLL_RESULT_JS,
            {"sel": sel_doubao, "before": 1, "mediaBefore": [], "kind": "video"},
        )
        assert res["done"] is True and res["urls"] == [VIDEO1], res
        # không có patterns → không done (giữ hành vi cũ khi config thiếu)
        res_nopat = page.evaluate(
            _POLL_RESULT_JS,
            {"sel": SEL, "before": 1, "mediaBefore": [], "kind": "video"},
        )
        assert res_nopat["done"] is False and res_nopat["urls"] == [VIDEO1], res_nopat
        # pattern xuất hiện nhưng CHƯA có video src → vẫn chưa done (contract)
        page.set_content(
            CHAT_PAGE.format(
                old=bot_msg(done=True, text="old"),
                new=(
                    '<div data-testid="receive_message">你的视频生成好了。'
                    '<div class="block-video-Db"><img class="cover-x" src="c.png"></div>'
                    "</div>"
                ),
            )
        )
        res_novideo = page.evaluate(
            _POLL_RESULT_JS,
            {"sel": sel_doubao, "before": 1, "mediaBefore": [], "kind": "video"},
        )
        assert res_novideo["done"] is False, res_novideo
        passed += 1
        print("PASS 12: Doubao video done-fallback via text pattern (no action bar)")

        # 13. Confirm-request: bot hỏi "Reply 'confirm'..." thay vì gen →
        # _is_confirm_request nhận ra (straight + curly quotes), poll trả
        # lastText để driver chỉ soi message CUỐI.
        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        driver = CiciDriver(load_config(str(cfg_path)))
        curly = ("I\u2019ll generate a stable locked-camera time-lapse. "
                 "Please confirm these parameters:\n- Duration: 10 seconds\n"
                 "Reply \u201cconfirm\u201d and I\u2019ll generate it directly.")
        assert driver._is_confirm_request(curly) is True, curly
        assert driver._is_confirm_request(
            'Reply "confirm" and I\'ll generate it directly.') is True
        assert driver._is_confirm_request("Hãy xác nhận để tôi tạo video.") is True
        # verify live 2026-08-22: Dola hỏi A/B/C duration + "reply “Generate”"
        generate_ask = (
            "I\u2019ll generate a stable locked-camera time-lapse. "
            "Video generation currently supports durations from 4 to 15 "
            "seconds. The default is 5 seconds. Do you want me to use:\n\n"
            "A. 5 seconds\nB. 10 seconds\nC. 15 seconds\n\n"
            "Or just reply \u201cGenerate\u201d and I\u2019ll make a 5-second "
            "version directly.")
        assert driver._is_confirm_request(generate_ask) is True, generate_ask
        # negative: text gen xong / refusal KHÔNG bị flag là confirm-request
        assert driver._is_confirm_request("Your video is ready.") is False
        assert driver._is_confirm_request(refusal) is False
        assert driver._is_confirm_request("") is False
        # "reply" trong văn xuôi (không nháy token) → không phải confirm-request
        assert driver._is_confirm_request(
            "I will reply to your request with the video shortly.") is False
        # poll trả lastText = text của bot message MỚI CUỐI (không phải gộp)
        ask = bot_msg(done=False, text="Reply \u201cconfirm\u201d and I\u2019ll generate it.")
        ok_msg = bot_msg(videos=[], done=False, text="Great, generating now.")
        page.set_content(CHAT_PAGE.format(old="", new=ask + ok_msg))
        res = page.evaluate(
            _POLL_RESULT_JS,
            {"sel": SEL, "before": 0, "mediaBefore": [], "kind": "video"},
        )
        assert res["lastText"] and "generating now" in res["lastText"], res
        assert driver._is_confirm_request(res["lastText"]) is False
        passed += 1
        print("PASS 13: confirm-request detected via lastText (curly/straight quotes)")

        # 14. Auto-reply selection: bot hỏi A/B/C → chữ cái khớp duration;
        # bot chỉ định token → dùng token; fallback "confirm".
        assert driver._auto_reply_text(generate_ask) == "Generate"
        assert driver._auto_reply_text(generate_ask, "5s") == "A"
        assert driver._auto_reply_text(generate_ask, "10s") == "B"
        assert driver._auto_reply_text(generate_ask, "15s") == "C"
        # duration không có trong options → dùng token của bot
        assert driver._auto_reply_text(generate_ask, "12s") == "Generate"
        # parameter sheet cũ: token "confirm" (curly quotes)
        assert driver._auto_reply_text(curly) == "confirm"
        # match pattern nhưng không có token → fallback "confirm"
        assert driver._auto_reply_text("Hãy xác nhận để tôi tạo video.") == "confirm"
        # VI token + VI đơn vị giây
        vi_ask = "Thời lượng video: A. 5 giây / B. 10 giây. Hãy trả lời \u201cTạo\u201d để bắt đầu."
        assert driver._auto_reply_text(vi_ask) == "Tạo"
        assert driver._auto_reply_text(vi_ask, "10s") == "B"
        # extract negatives: không có chỉ dẫn reply / văn xuôi "5 seconds"
        assert driver._extract_reply_token("Your video is ready.") is None
        assert driver._duration_choice(
            "The default is 5 seconds.", "5s") is None
        passed += 1
        print("PASS 14: auto-reply picks duration letter / bot token / fallback")

        # 15. Choice question KHÔNG kèm lệnh reply ("Which duration do you
        # want?" — verify live 2026-08-22, markdown bold options) vẫn detect
        # structural + auto-reply option đầu (5s default) khi không có -d.
        choice_ask = (
            "I\u2019ll generate a stable locked-camera time-lapse with fast "
            "cloud/light movement and a fixed horizon. I need one more "
            "parameter from you:\n\n"
            "**A. 5 seconds**\n**B. 10 seconds**\n**C. 15 seconds**\n\n"
            "Which duration do you want?")
        assert driver._is_choice_question(choice_ask) is True, choice_ask
        assert driver._is_confirm_request(choice_ask) is True, choice_ask
        assert driver._auto_reply_text(choice_ask) == "A"
        assert driver._auto_reply_text(choice_ask, "10s") == "B"
        assert driver._auto_reply_text(choice_ask, "15s") == "C"
        # option đơn lẻ trong văn xuôi / "Duration: X" không phải choice q
        assert driver._is_choice_question(
            "I will make a 5-second version directly.") is False
        assert driver._is_confirm_request(
            "Please set Duration: 10 seconds and I'll generate.") is False
        passed += 1
        print("PASS 15: choice question without reply-token detected + answered")
    finally:
        browser.close()
        pw.stop()

    print(f"\n{passed}/15 tests passed")
    return 0 if passed == 15 else 1


if __name__ == "__main__":
    sys.exit(main())
