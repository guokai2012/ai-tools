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


async def capture_lyrics_flow(page, vp_name):
    """听文档页 → 点击「转歌词」按钮 → 等生成完成 → 截图对话框。"""
    await navigate(page, "/")
    # 等待第一条听文档渲染
    await page.wait_for_selector(".library-item", timeout=5000)
    # 截图：列表带「转歌词」按钮的状态
    await page.screenshot(path=str(OUT_DIR / f"lyrics-list-{vp_name}.png"),
                          full_page=True)
    print(f"  saved lyrics-list-{vp_name}.png")

    # 点击第一个转歌词按钮
    lyrics_btn = await page.query_selector(".lib-lyrics")
    if lyrics_btn is None:
        print(f"  WARN: no .lib-lyrics button on {vp_name}")
        return
    await lyrics_btn.click()
    # 给 M3 mock 一个足够的时间完成（最长 4 秒）
    try:
        await page.wait_for_selector("#lyricsDialogPreview", timeout=2000)
    except Exception:
        pass
    # 等到下载链接出现说明生成已完成
    try:
        await page.wait_for_selector("#lyricsDialogDownload:not([hidden])", timeout=5000)
    except Exception:
        # 仍在生成，截一张中间状态
        await page.screenshot(path=str(OUT_DIR / f"lyrics-dialog-loading-{vp_name}.png"),
                              full_page=True)
        print(f"  saved lyrics-dialog-loading-{vp_name}.png")
        return

    # 等预览文字填充完整
    await page.wait_for_timeout(400)
    await page.screenshot(path=str(OUT_DIR / f"lyrics-dialog-{vp_name}.png"),
                          full_page=True)
    print(f"  saved lyrics-dialog-{vp_name}.png")


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

            # 歌词流程（点「转歌词」按钮 + 弹窗截图）
            await capture_lyrics_flow(page, vp_name)

            await ctx.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())