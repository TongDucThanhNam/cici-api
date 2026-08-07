"""Connect to running Cici via CDP and dump DOM structure of the chat UI.
Read-only inspection — does not click or type."""
import asyncio
import json
from playwright.async_api import async_playwright

CDP_URL = "http://127.0.0.1:9222"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        print(f"Connected. Contexts: {len(browser.contexts)}")
        target = None
        for ctx in browser.contexts:
            for pg in ctx.pages:
                print(f"  page: {pg.url}")
                if "dola-chat/chat" in pg.url or pg.url.endswith("/chat"):
                    target = pg
        if not target:
            # fallback: last page of first context
            target = browser.contexts[0].pages[-1]
        print(f"\n>>> Target page: {target.url}")
        await target.bring_to_front()

        # Snapshot key interactive elements via DOM query in-page.
        probe = r"""
        () => {
          const out = { textarea: [], input: [], buttons: [], editable: [], links: [], images: [] };
          document.querySelectorAll('textarea').forEach((e,i)=>{
            out.textarea.push({i, placeholder:e.placeholder, className:e.className,
                               id:e.id, dataTestId:e.getAttribute('data-testid'),
                               ariaLabel:e.getAttribute('aria-label'),
                               rect: e.getBoundingClientRect().width|0+'x'+(e.getBoundingClientRect().height|0)});
          });
          document.querySelectorAll('input[type="text"], input:not([type])').forEach((e,i)=>{
            if (e.offsetParent !== null || e.offsetHeight > 0)
              out.input.push({i, placeholder:e.placeholder, className:e.className, ariaLabel:e.getAttribute('aria-label'),
                              dataTestId:e.getAttribute('data-testid')});
          });
          document.querySelectorAll('div[contenteditable="true"]').forEach((e,i)=>{
            out.editable.push({i, className:e.className, dataTestId:e.getAttribute('data-testid'),
                               ariaLabel:e.getAttribute('aria-label'),
                               rect: e.getBoundingClientRect().width|0+'x'+(e.getBoundingClientRect().height|0)});
          });
          document.querySelectorAll('button, [role="button"]').forEach((e,i)=>{
            const txt = (e.innerText||'').trim().slice(0,30);
            const label = e.getAttribute('aria-label')||'';
            const tt = e.getAttribute('data-testid')||e.getAttribute('data-type')||'';
            if (txt || label || tt)
              out.buttons.push({i, txt, label, testid:tt, className:e.className.slice(0,60)});
          });
          document.querySelectorAll('a').forEach((e,i)=>{
            const t=(e.innerText||'').trim().slice(0,20);
            if(t) out.links.push({i, t, href:(e.getAttribute('href')||'').slice(0,60)});
          });
          // Look for image/video result containers + mode switch hints
          document.querySelectorAll('img').forEach((e,i)=>{
            const src=(e.currentSrc||e.src||'').slice(0,80);
            if(src && !src.startsWith('data:')) out.images.push({i, src, className:e.className.slice(0,40)});
          });
          out.bodyTextSample = (document.body.innerText||'').slice(0,400);
          return out;
        }
        """
        result = await target.evaluate(probe)
        print("\n=== DOM PROBE RESULT ===")
        print(json.dumps(result, indent=2, ensure_ascii=False)[:6000])


asyncio.run(main())
