"""Inspect Cici/Dola skill-mode UI (image & video) for automation upgrades.

Semi-read-only: attaches via CDP, opens the image/video skill modes and dumps
the toolbar DOM (model options, ratio/size controls, reference buttons,
hidden file inputs). Does NOT type a prompt or click send — no generation,
no quota consumed. Close menus with Escape, never submit.
"""
import asyncio
import json

from playwright.async_api import async_playwright

CDP_URL = "http://127.0.0.1:9222"
CHAT_PAT = "dola-chat/chat"

SKILL_IMAGE = 'button[data-testid="skill_bar_button_3"]'
SKILL_VIDEO = 'button[data-testid="skill_bar_button_17"]'

DUMP_JS = r"""
() => {
  const out = {buttons: [], fileInputs: [], skillBar: []};
  document.querySelectorAll('button, [role="button"]').forEach(e => {
    const txt = (e.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 40);
    const tt = e.getAttribute('data-testid') || '';
    if (txt || tt) out.buttons.push({txt, testid: tt});
  });
  document.querySelectorAll('input[type="file"]').forEach((e, i) => {
    out.fileInputs.push({
      i,
      accept: e.getAttribute('accept') || null,
      files: e.files.length,
    });
  });
  document.querySelectorAll('[data-testid^="skill_bar_button"]').forEach(e => {
    out.skillBar.push({
      testid: e.getAttribute('data-testid'),
      txt: (e.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 40),
    });
  });
  return out;
}
"""


async def dump(page, label: str) -> dict:
    res = await page.evaluate(DUMP_JS)
    print(f"\n===== {label} =====")
    print(json.dumps(res, indent=2, ensure_ascii=False)[:8000])
    return res


async def dump_model_menu(page) -> None:
    """Open the Model dropdown and dump its options (Radix menu)."""
    try:
        btn = page.get_by_role("main").locator('button', has_text="Model").first
        if await btn.count() == 0:
            print("  (no 'Model' button found)")
            return
        await btn.click()
        await asyncio.sleep(0.8)
        items = await page.locator('[role="menuitem"]').all_inner_texts()
        print("  Model menu options:", [t.strip() for t in items])
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
    except Exception as e:  # noqa: BLE001
        print(f"  Model menu dump failed: {e}")


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        target = None
        for ctx in browser.contexts:
            for pg in ctx.pages:
                if CHAT_PAT in pg.url:
                    target = pg
        if target is None:
            target = browser.contexts[0].pages[-1]
        print(f">>> Target page: {target.url}")
        await target.bring_to_front()
        await asyncio.sleep(1)

        await dump(target, "initial chat page")

        print("\n>>> Entering IMAGE skill mode…")
        await target.locator(SKILL_IMAGE).first.click()
        await asyncio.sleep(1.5)
        await dump(target, "IMAGE skill mode toolbar")
        await dump_model_menu(target)

        print("\n>>> Entering VIDEO skill mode…")
        await target.locator(SKILL_VIDEO).first.click()
        await asyncio.sleep(1.5)
        await dump(target, "VIDEO skill mode toolbar")
        await dump_model_menu(target)


asyncio.run(main())
