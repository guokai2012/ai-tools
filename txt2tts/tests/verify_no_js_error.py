"""用 Playwright 加载首页，捕获 JS 报错并截图。"""
import asyncio
from playwright.async_api import async_playwright


async def main():
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await ctx.new_page()

        page.on("console", lambda msg: errors.append(("console", msg.type, msg.text))
                if msg.type == "error" else None)
        page.on("pageerror", lambda exc: errors.append(("pageerror", "", str(exc))))

        await page.goto("http://127.0.0.1:8000/static/index.html")
        await page.wait_for_timeout(2000)

        # 也测试设置页
        await page.evaluate("location.hash = '#/settings'")
        await page.wait_for_timeout(1000)

        # 测试上传页
        await page.evaluate("location.hash = '#/upload'")
        await page.wait_for_timeout(1000)

        # 测试听文档页（默认）
        await page.evaluate("location.hash = '#/'")
        await page.wait_for_timeout(1000)

        await page.screenshot(path="tests/screenshots/no-js-error.png", full_page=True)
        await browser.close()

    print(f"JS errors captured: {len(errors)}")
    for kind, level, msg in errors:
        print(f"  [{kind}/{level}] {msg[:200]}")
    if not errors:
        print("✓ No JS errors")


asyncio.run(main())