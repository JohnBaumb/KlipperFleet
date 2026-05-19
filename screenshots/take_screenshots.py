"""
Take screenshots of the KlipperFleet UI using Playwright.
Spins up the mock server, navigates to each view, and saves PNGs to images/.
"""
import asyncio
import subprocess
import sys
import time
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent.parent / "images"
MOCK_SERVER = Path(__file__).parent / "mock_server.py"
BASE_URL = "http://127.0.0.1:8321"
VIEWPORT = {"width": 1400, "height": 900}


async def wait_for_server(url: str, timeout: float = 10.0):
    """Wait for the mock server to become responsive."""
    import httpx

    deadline = time.time() + timeout
    async with httpx.AsyncClient() as client:
        while time.time() < deadline:
            try:
                r = await client.get(f"{url}/api/health")
                if r.status_code == 200:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.2)
    raise TimeoutError(f"Mock server at {url} did not start within {timeout}s")


async def take_screenshots():
    from playwright.async_api import async_playwright

    OUTPUT_DIR.mkdir(exist_ok=True)

    # Start mock server
    server_proc = subprocess.Popen(
        [sys.executable, str(MOCK_SERVER)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        await wait_for_server(BASE_URL)
        print("Mock server is ready.")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport=VIEWPORT)

            # --- Dashboard ---
            await page.goto(BASE_URL)
            # Wait for Vue to mount and data to load
            await page.wait_for_timeout(2000)
            # Click Dashboard nav if not already active
            dashboard_btn = page.locator("text=Dashboard").first
            if await dashboard_btn.count() > 0:
                await dashboard_btn.click()
                await page.wait_for_timeout(1000)

            # Click "Build & Flash All" to populate the log pane with realistic output
            build_flash_btn = page.locator("button:has-text('Build & Flash All')").first
            if await build_flash_btn.count() > 0:
                await build_flash_btn.click()
                # Wait for batch to complete and logs to render
                await page.wait_for_timeout(2500)

            await page.screenshot(path=str(OUTPUT_DIR / "dashboard.png"), full_page=False)
            print("Captured: dashboard.png")

            # --- Configurator ---
            config_btn = page.locator("text=Configurator").first
            if await config_btn.count() > 0:
                await config_btn.click()
                await page.wait_for_timeout(1500)
            await page.screenshot(path=str(OUTPUT_DIR / "configurator.png"), full_page=False)
            print("Captured: configurator.png")

            # --- Fleet Manager ---
            fleet_btn = page.locator("text=Fleet Manager").first
            if await fleet_btn.count() > 0:
                await fleet_btn.click()
                await page.wait_for_timeout(1500)
            await page.screenshot(path=str(OUTPUT_DIR / "fleet_manager.png"), full_page=False)
            print("Captured: fleet_manager.png")

            await browser.close()

        print(f"\nAll screenshots saved to: {OUTPUT_DIR.resolve()}")

    finally:
        server_proc.terminate()
        server_proc.wait(timeout=5)


if __name__ == "__main__":
    asyncio.run(take_screenshots())
