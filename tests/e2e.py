"""Playwright walk-through of the main UI flow against a running instance.

Point it at a server whose library is the fixture library (tests/mkfix.py):
    python tests/e2e.py [base url] [light|dark]
Defaults: http://127.0.0.1:8095/ light. Screenshots go to ./test-output/.
Run on a fresh library: it quarantines files and then undoes it.
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8095/"
SCHEME = sys.argv[2] if len(sys.argv) > 2 else "light"
OUT = "test-output"


async def main():
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        # CHROMIUM_PATH lets you use an already-installed browser build.
        b = await p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        pg = await b.new_page(viewport={"width": 1280, "height": 1000}, color_scheme=SCHEME)
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errs.append(m.text))
        await pg.goto(URL)
        await pg.click("#scan")
        await pg.wait_for_selector(".album", timeout=20000)
        await pg.click("#expand-all")
        await pg.screenshot(path=f"{OUT}/1-suggested-{SCHEME}.png", full_page=True)

        await pg.click("[data-sec=manual]")
        await pg.wait_for_timeout(200)
        await pg.click(".album [data-act=tags]")
        await pg.click(".album [data-play]")
        await pg.wait_for_timeout(500)
        await pg.screenshot(path=f"{OUT}/2-review-{SCHEME}.png", full_page=True)

        # Edition select-all on the different-artists card, then the guard.
        kk = pg.locator(".album", has_text="Kings Kaleidoscope")
        await kk.locator("input[data-ed='0']").check()
        await pg.wait_for_timeout(100)
        n1 = await kk.locator("input[data-rel]:checked").count()
        await kk.locator("input[data-ed='1']").click()
        await pg.wait_for_timeout(100)
        n2 = await kk.locator("input[data-rel]:checked").count()
        print("guard: selected", n1, "after 2nd edition", n2, "|", await pg.text_content("#toast-msg"))
        assert n1 == n2 == 1

        await pg.locator(".album", has_text="Josh Garrels").locator("[data-act=keep]").click()
        await pg.wait_for_timeout(300)
        print("kept", await pg.text_content("#n-kept"), "review", await pg.text_content("#n-manual"))

        await pg.click("[data-sec=suggested]")
        await pg.wait_for_timeout(100)
        print("bar:", await pg.text_content("#count"))
        await pg.click("#go")
        await pg.wait_for_timeout(800)
        print("toast:", await pg.text_content("#toast-msg"))
        await pg.click("#toast-act")
        await pg.wait_for_timeout(800)
        print("after undo:", await pg.text_content("#toast-msg"))

        await pg.set_viewport_size({"width": 390, "height": 900})
        await pg.click("[data-sec=manual]")
        await pg.wait_for_timeout(200)
        await pg.screenshot(path=f"{OUT}/3-mobile-{SCHEME}.png")
        print("errors:", errs)
        assert not errs
        await b.close()

asyncio.run(main())
