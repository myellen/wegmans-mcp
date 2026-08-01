"""Interactive Wegmans login flow.

Shared by `scripts/setup_login.py` (terminal) and the `setup_wegmans_login`
MCP tool (Claude Desktop, where there is no terminal). Opens a real headed
browser window; the user signs in; the session state is saved for the
silent-renewal minting in auth.py.

If the Playwright Chromium build is missing (fresh install from the .mcpb
bundle), it is downloaded automatically before the window opens.
"""

from __future__ import annotations

import asyncio
import re
import json
import sys
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import Error as PlaywrightError, async_playwright

from .auth import API_HOST_SUBSTRING, _is_b2c_token

LOGIN_URL = "https://www.meals2go.com/"
SHOP_URL = "https://www.wegmans.com/"
LOYALTY_URL_RE = re.compile(r"/loyalty/(\d+)")

StatusFn = Callable[[str, str], None]


def _noop_status(state: str, detail: str) -> None:  # pragma: no cover
    pass


def _read_env_values(path: Path) -> dict[str, str]:
    """Parse an existing simple .env into a dict (empty if absent/unreadable)."""
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def write_env_var(path: Path, key: str, value: str) -> None:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")


def parse_shopping_context(raw: str | None) -> dict[str, Any]:
    """Pull the home store / shopping method out of wegmans.com's
    `shopping-context-storage` localStorage blob. Shape:
    {"state": {"storeNumber": 91, "shoppingMethod": "instore",
               "storeDetails": {"storeName": "Amherst St", ...}}}
    """
    if not raw:
        return {}
    try:
        state = (json.loads(raw) or {}).get("state") or {}
    except (ValueError, AttributeError):
        return {}
    out: dict[str, Any] = {}
    store_number = state.get("storeNumber")
    if isinstance(store_number, (int, str)) and str(store_number).isdigit():
        out["store_id"] = int(store_number)
    method = state.get("shoppingMethod") or state.get("shoppingMethodUI")
    # wegmans.com names the channels instore/pickup/delivery; the cart API
    # (and our tools) use store/curbside/delivery.
    mapped = {"instore": "store", "pickup": "curbside", "delivery": "delivery"}
    if method in mapped:
        out["fulfillment_type"] = mapped[method]
    name = (state.get("storeDetails") or {}).get("storeName")
    if name:
        out["store_name"] = name
    return out


async def _read_shopping_context(page) -> dict[str, Any]:
    try:
        raw = await page.evaluate(
            "() => window.localStorage.getItem('shopping-context-storage')"
        )
    except Exception:
        return {}
    return parse_shopping_context(raw)


async def _install_chromium() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "playwright", "install", "chromium",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"playwright install chromium exited with {rc}")


async def run_login(
    auth_file: Path,
    shop_auth_file: Path,
    env_file: Path | None = None,
    on_status: StatusFn = _noop_status,
    signin_timeout: int = 600,
) -> dict[str, Any]:
    """Run the full interactive login. Returns a summary dict:

    {ok, shop_ok, loyalty_id, saved: [paths]}

    Raises on hard failures (browser can't start, user never signed in).
    """
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    shop_auth_file.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=False)
        except PlaywrightError as e:
            if "Executable doesn't exist" not in str(e):
                raise
            on_status("installing_browser",
                      "Downloading Chromium (one-time, ~2 minutes)...")
            await _install_chromium()
            browser = await pw.chromium.launch(headless=False)

        try:
            context = await browser.new_context()
            page = await context.new_page()

            token_ready = asyncio.Event()
            closed = asyncio.Event()
            loyalty: dict[str, str] = {}

            # Fail fast if the user closes the window instead of waiting the
            # full sign-in timeout with no way to retry.
            browser.on("disconnected", lambda _b: closed.set())
            page.on("close", lambda _p: closed.set())

            def on_request(req):
                if API_HOST_SUBSTRING not in req.url:
                    return
                if not token_ready.is_set():
                    auth = req.headers.get("authorization")
                    if auth and auth.lower().startswith("bearer "):
                        if _is_b2c_token(auth.split(" ", 1)[1]):
                            token_ready.set()
                if "id" not in loyalty:
                    m = LOYALTY_URL_RE.search(req.url)
                    if m:
                        loyalty["id"] = m.group(1)

            page.on("request", on_request)
            await page.goto(LOGIN_URL)
            on_status("waiting_for_signin",
                      "Browser window open — click Sign In on meals2go.com "
                      "and log in to your Wegmans account.")

            waiters = [
                asyncio.create_task(token_ready.wait()),
                asyncio.create_task(closed.wait()),
            ]
            try:
                done, _ = await asyncio.wait(
                    waiters, timeout=signin_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for w in waiters:
                    w.cancel()
            if not token_ready.is_set():
                if closed.is_set():
                    raise RuntimeError(
                        "The sign-in window was closed before login "
                        "completed. Run the login again to retry."
                    )
                raise RuntimeError(
                    f"No sign-in detected after {signin_timeout // 60} minutes; "
                    "closing the browser. Run the login again to retry."
                )

            # Give the home page a moment to fire the digital-coupons request
            # that carries the loyalty number.
            for _ in range(30):
                if "id" in loyalty:
                    break
                await asyncio.sleep(0.5)

            state = await context.storage_state()
            auth_file.write_text(json.dumps(state, indent=2))

            # Warm the grocery site so its (separate) MSAL client signs in
            # off the same B2C session; only then is auth-shop.json valid.
            on_status("warming_shop", "Signing in to the grocery site (wegmans.com)...")
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
            except Exception:
                shop_ok = False

            # wegmans.com keeps the signed-in shopper's home store and
            # shopping method in localStorage — read it so the user never has
            # to look up a store number by hand.
            store = await _read_shopping_context(page) if shop_ok else {}

            state = await context.storage_state()
            auth_file.write_text(json.dumps(state, indent=2))
            saved = [str(auth_file)]
            if shop_ok:
                # A failed warm-up must not clobber a working auth-shop.json
                # from a previous run.
                shop_auth_file.write_text(json.dumps(state, indent=2))
                saved.append(str(shop_auth_file))

            kept_store: int | None = None
            if env_file is not None:
                wrote_env = False
                if "id" in loyalty:
                    write_env_var(env_file, "WEGMANS_LOYALTY_ID", loyalty["id"])
                    wrote_env = True

                # A store the user deliberately chose outranks the account's
                # own home store — re-logging in after a session expires must
                # not silently move them back.
                existing = _read_env_values(env_file)
                user_chose = existing.get("WEGMANS_STORE_SOURCE") == "user"
                user_store = existing.get("WEGMANS_STORE_ID")
                if user_chose and user_store and str(user_store) != str(store.get("store_id")):
                    kept_store = int(user_store) if str(user_store).isdigit() else None
                else:
                    if store.get("store_id"):
                        write_env_var(env_file, "WEGMANS_STORE_ID",
                                      str(store["store_id"]))
                        wrote_env = True
                    if store.get("fulfillment_type"):
                        write_env_var(env_file, "WEGMANS_FULFILLMENT_TYPE",
                                      store["fulfillment_type"])
                        wrote_env = True
                if wrote_env:
                    saved.append(str(env_file))

            return {
                "ok": True,
                "shop_ok": shop_ok,
                "loyalty_id": loyalty.get("id"),
                "store_id": store.get("store_id"),
                "store_name": store.get("store_name"),
                "fulfillment_type": store.get("fulfillment_type"),
                # Set when the user's remembered store beat the detected one.
                "kept_store_id": kept_store,
                "saved": saved,
            }
        finally:
            await browser.close()
