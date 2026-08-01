"""One-time interactive Wegmans Meals2Go login.

Opens a real browser window. Log in normally. The script:
  1. Waits until an Azure B2C token appears on wegapi.azure-api.net
     (i.e. you've successfully signed in).
  2. Watches for the digital-coupons URL `/loyalty/<id>` to extract your
     Shoppers Club number (fires automatically on home-page load).
  3. Saves the browser session to auth.json and your loyalty number to
     a local .env file.

Run: `uv run python scripts/setup_login.py`
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

AUTH_FILE = Path("auth.json")
SHOP_AUTH_FILE = Path("auth-shop.json")
ENV_FILE = Path(".env")
LOGIN_URL = "https://www.meals2go.com/"
SHOP_URL = "https://www.wegmans.com/"
API_HOST_SUBSTRING = "wegapi.azure-api.net"
B2C_ISSUER_SUBSTRING = "myaccount.wegmans.com"
LOYALTY_URL_RE = re.compile(r"/loyalty/(\d+)")


def _is_b2c_token(jwt: str) -> bool:
    try:
        payload_b64 = jwt.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        return B2C_ISSUER_SUBSTRING in payload.get("iss", "")
    except Exception:
        return False


def _write_env_var(path: Path, key: str, value: str) -> None:
    """Upsert KEY=VALUE in a simple .env file."""
    lines = path.read_text().splitlines() if path.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        token_ready = asyncio.Event()
        loyalty_id: dict[str, str] = {}

        def on_request(req):
            if API_HOST_SUBSTRING not in req.url:
                return
            if not token_ready.is_set():
                auth = req.headers.get("authorization")
                if auth and auth.lower().startswith("bearer "):
                    if _is_b2c_token(auth.split(" ", 1)[1]):
                        token_ready.set()
            if "loyalty_id" not in loyalty_id:
                m = LOYALTY_URL_RE.search(req.url)
                if m:
                    loyalty_id["loyalty_id"] = m.group(1)

        page.on("request", on_request)
        await page.goto(LOGIN_URL)

        print("Browser opened. Click 'Sign In' and log in to your Wegmans account.")
        print("(Waiting for an Azure B2C token ...)")

        try:
            await asyncio.wait_for(token_ready.wait(), timeout=600)
        except asyncio.TimeoutError:
            print("Timed out after 10 minutes. Closing.", file=sys.stderr)
            await browser.close()
            return 1

        # Give the home page ~15s to fire the digital-coupons request that
        # carries the loyalty number. If it doesn't fire, we still save auth.
        for _ in range(30):
            if "loyalty_id" in loyalty_id:
                break
            await asyncio.sleep(0.5)

        # Warm the grocery side too. wegmans.com uses a different MSAL client
        # than Meals2Go, so its tokens/cookies only land in storage state once
        # that SPA has completed its own silent sign-in against the shared B2C
        # session. Without this the saved state can order prepared food but
        # not groceries.
        print("Signing in to the grocery site (wegmans.com) ...")
        shop_ok = False
        try:
            await page.goto(SHOP_URL, wait_until="domcontentloaded")
            for _ in range(40):
                await asyncio.sleep(0.5)
                shop_ok = await page.evaluate(
                    """() => Object.keys(window.localStorage)
                             .some(k => k.includes('login.windows')
                                     || k.includes('msal')
                                     || k.includes('accesstoken'))"""
                )
                if shop_ok:
                    break
            else:
                print(
                    "Grocery-site sign-in didn't complete; grocery cart tools "
                    "may need another run. Catalog search will still work.",
                    file=sys.stderr,
                )
        except Exception as e:  # non-fatal: Meals2Go auth is already captured
            print(f"Grocery-site warm-up failed ({e}).", file=sys.stderr)

        state = await context.storage_state()
        AUTH_FILE.write_text(json.dumps(state, indent=2))
        saved = [str(AUTH_FILE)]
        if shop_ok:
            # Only overwrite the shop session when the warm-up actually
            # signed in — a failed warm-up must not clobber a working
            # auth-shop.json from a previous run.
            SHOP_AUTH_FILE.write_text(json.dumps(state, indent=2))
            saved.append(str(SHOP_AUTH_FILE))
        elif SHOP_AUTH_FILE.exists():
            print(f"Kept existing {SHOP_AUTH_FILE} untouched.", file=sys.stderr)
        print(f"Auth saved to {' and '.join(saved)}.")

        if "loyalty_id" in loyalty_id:
            _write_env_var(ENV_FILE, "WEGMANS_LOYALTY_ID", loyalty_id["loyalty_id"])
            print(f"Loyalty number saved to {ENV_FILE}.")
        else:
            print(
                "Couldn't detect your loyalty number automatically. "
                "Set WEGMANS_LOYALTY_ID manually if you want to use coupon tools.",
                file=sys.stderr,
            )

        await browser.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
