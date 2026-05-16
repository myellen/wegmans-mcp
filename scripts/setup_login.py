"""One-time interactive Wegmans Meals2Go login.

Opens a real browser window. Log in normally. The script waits until it
sees a successful authenticated API call on wegapi.azure-api.net, then
saves the browser context (cookies + localStorage) to auth.json.

Run: `uv run python scripts/setup_login.py`
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

AUTH_FILE = Path("auth.json")
LOGIN_URL = "https://www.meals2go.com/"
API_HOST_SUBSTRING = "wegapi.azure-api.net"
B2C_ISSUER_SUBSTRING = "myaccount.wegmans.com"


def _is_b2c_token(jwt: str) -> bool:
    try:
        payload_b64 = jwt.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        return B2C_ISSUER_SUBSTRING in payload.get("iss", "")
    except Exception:
        return False


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        ready = asyncio.Event()

        def on_request(req):
            if ready.is_set() or API_HOST_SUBSTRING not in req.url:
                return
            auth = req.headers.get("authorization")
            if auth and auth.lower().startswith("bearer "):
                jwt = auth.split(" ", 1)[1]
                if _is_b2c_token(jwt):
                    ready.set()

        page.on("request", on_request)
        await page.goto(LOGIN_URL)

        print("Browser opened. Click 'Sign In' and log in to your Wegmans account.")
        print("(Waiting for an Azure B2C token to appear on wegapi.azure-api.net ...)")

        try:
            await asyncio.wait_for(ready.wait(), timeout=600)
        except asyncio.TimeoutError:
            print("Timed out after 10 minutes. Closing.", file=sys.stderr)
            await browser.close()
            return 1

        state = await context.storage_state()
        AUTH_FILE.write_text(json.dumps(state, indent=2))
        print(f"Auth saved to {AUTH_FILE}.")
        await browser.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
