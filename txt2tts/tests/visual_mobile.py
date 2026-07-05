"""Playwright-based mobile/tablet/desktop visual smoke test."""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

OUT_DIR = Path(__file__).parent / "screenshots"
OUT_DIR.mkdir(exist_ok=True)

VIEWPORTS = [
    ("desktop", 1280, 800),
    ("tablet", 768, 1024),
    ("mobile", 390, 844),   # iPhone 14
    ("small_mobile", 360, 780),
]


async def navigate(page, hash_route):
    """通过 evaluate 修改 hash 来触发 hashchange 事件（等价于用户点菜单）。"""
    await page.evaluate(f"location.hash = '#{hash_route}'")
    # 让 router 完成视图切换 + 异步 fetch
    await page.wait_for_timeout(900)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for vp_name, w, h in VIEWPORTS:
            ctx = await browser.new_context(viewport={"width": w, "height": h},
                                            device_scale_factor=2)
            page = await ctx.new_page()

            # 先访问一次主页，让 router 初始化
            await page.goto("http://127.0.0.1:8765/static/index.html")
            await page.wait_for_timeout(400)

            # 听文档
            await navigate(page, "/")
            await page.screenshot(path=str(OUT_DIR / f"listen-{vp_name}.png"),
                                  full_page=True)
            print(f"  saved listen-{vp_name}.png")

            # 上传转语音（任务列表）
            await navigate(page, "/upload")
            await page.screenshot(path=str(OUT_DIR / f"upload-{vp_name}.png"),
                                  full_page=True)
            print(f"  saved upload-{vp_name}.png")

            # 对话框（点击新增转语音）
            await page.click("#newTaskBtn")
            await page.wait_for_timeout(400)
            await page.screenshot(path=str(OUT_DIR / f"dialog-{vp_name}.png"),
                                  full_page=True)
            print(f"  saved dialog-{vp_name}.png")
            await page.click("#dialogCloseBtn")
            await page.wait_for_timeout(200)

            # 任务详情（先取一个 task_id）
            await navigate(page, "/upload")
            await page.wait_for_timeout(400)
            btn = await page.query_selector(".task-detail-btn")
            if btn:
                tid = await btn.get_attribute("data-task-id")
                await navigate(page, f"/task/{tid}")
                await page.screenshot(path=str(OUT_DIR / f"task-{vp_name}.png"),
                                      full_page=True)
                print(f"  saved task-{vp_name}.png")

            await ctx.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())