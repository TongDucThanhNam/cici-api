"""Read-only probe: inspect DOM quanh ảnh kết quả (mdbox_image) + action bar.

Không click, không type — chỉ evaluate. Mục đích: tìm nơi UI expose URL ảnh
GỐC (không phải preview downsize_watermark 288px).
"""
import asyncio
import json

from playwright.async_api import async_playwright

CDP_URL = "http://127.0.0.1:9222"

PROBE = r"""
() => {
  const out = {url: location.url || location.href, mdbox: [], actionBars: [], otherLinks: []};
  const ml = document.querySelector('[data-testid="message-list"]');
  if (!ml) return {error: 'no message-list', href: location.href};
  const recvs = Array.from(ml.querySelectorAll('[data-testid="receive_message"]'));
  out.recvCount = recvs.length;
  recvs.slice(-2).forEach((m, mi) => {
    m.querySelectorAll('[data-testid="mdbox_image"]').forEach((b, bi) => {
      const img = b.querySelector('img');
      const chain = [];
      let n = b;
      for (let d = 0; d < 6 && n; d++, n = n.parentElement) {
        chain.push({
          tag: n.tagName,
          testid: n.getAttribute ? n.getAttribute('data-testid') : null,
          href: n.getAttribute ? n.getAttribute('href') : null,
        });
      }
      // mọi href/download xuất hiện trong hộp ảnh
      const links = Array.from(b.querySelectorAll('a')).map(a => a.getAttribute('href'));
      out.mdbox.push({
        mi, bi,
        imgAttrs: img ? Array.from(img.attributes).map(a => [a.name, (a.value || '').slice(0, 150)]) : null,
        imgNatural: img ? (img.naturalWidth + 'x' + img.naturalHeight) : null,
        boxAttrs: Array.from(b.attributes).map(a => [a.name, (a.value || '').slice(0, 80)]),
        linksInBox: links,
        chain,
      });
    });
    const bar = m.querySelector('[data-testid="message_action_bar"]');
    if (bar) {
      out.actionBars.push({
        mi,
        buttons: Array.from(bar.querySelectorAll('button,[role="button"],a')).slice(0, 12).map(e => ({
          tag: e.tagName,
          testid: e.getAttribute('data-testid'),
          aria: e.getAttribute('aria-label'),
          href: e.getAttribute('href'),
          html: e.outerHTML.slice(0, 110),
        })),
      });
    }
  });
  // link download/attachment nào khác trong message-list
  ml.querySelectorAll('a[href]').forEach(a => {
    const h = a.getAttribute('href') || '';
    if (/download|image|jpeg|png|tplv|tos/i.test(h)) out.otherLinks.push(h.slice(0, 160));
  });
  return out;
}
"""


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        target = None
        for ctx in browser.contexts:
            for pg in ctx.pages:
                if "dola-chat/chat" in pg.url:
                    target = pg
        if not target:
            target = browser.contexts[0].pages[-1]
        print(f">>> page: {target.url}")
        result = await target.evaluate(PROBE)
        print(json.dumps(result, indent=1, ensure_ascii=False)[:9000])


asyncio.run(main())
