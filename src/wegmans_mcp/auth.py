"""Mints Meals2Go bearer JWTs by replaying a logged-in browser session.

Strategy: a one-time interactive login (scripts/setup_login.py) saves the
browser's cookies + localStorage to `auth.json`. At runtime, we open a
headless Chromium with that storage state, load meals2go.com so MSAL.js
silently re-mints a token, and grab the Authorization header off the next
authenticated API request.

Why not pure MSAL Python? The Meals2Go SPA does not request the
`offline_access` scope, so we cannot get a refresh token through the
public auth-code flow. Replaying the SPA's silent-renewal flow is the
robust path.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import async_playwright

TOKEN_TRIGGER_URL = "https://www.meals2go.com/"
API_HOST_SUBSTRING = "wegapi.azure-api.net"
B2C_ISSUER_SUBSTRING = "myaccount.wegmans.com"

# The grocery site (wegmans.com) is a different MSAL client with its own
# token cache, so it needs its own storage-state file and trigger URL. Its
# SPA fires authenticated calls to the commerce backend on load, which is
# where we harvest the token.
SHOP_TRIGGER_URL = "https://www.wegmans.com/"
SHOP_API_HOST_SUBSTRING = "api.digitaldevelopment.wegmans.cloud"
SHOP_AUTH_FILE = Path("auth-shop.json")

DEFAULT_AUTH_FILE = Path("auth.json")
TOKEN_REFRESH_LEEWAY_SEC = 300  # refresh 5 min before expiry


def _is_b2c_token(jwt: str) -> bool:
    try:
        payload_b64 = jwt.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        return B2C_ISSUER_SUBSTRING in payload.get("iss", "")
    except Exception:
        return False


@dataclass
class CachedToken:
    jwt: str
    expires_at: float

    def expired(self, leeway: int = TOKEN_REFRESH_LEEWAY_SEC) -> bool:
        return time.time() + leeway >= self.expires_at


def _decode_exp(jwt: str) -> float:
    payload_b64 = jwt.split(".")[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    return float(payload["exp"])


class WegmansAuth:
    def __init__(
        self,
        auth_file: Path = DEFAULT_AUTH_FILE,
        trigger_url: str = TOKEN_TRIGGER_URL,
        api_host_substring: str = API_HOST_SUBSTRING,
    ):
        self.auth_file = auth_file
        self.trigger_url = trigger_url
        self.api_host_substring = api_host_substring
        self._token: CachedToken | None = None
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            if self._token and not self._token.expired():
                return self._token.jwt
            self._token = await self._mint()
            return self._token.jwt

    async def _mint(self) -> CachedToken:
        if not self.auth_file.exists():
            raise RuntimeError(
                f"Auth file {self.auth_file} missing. "
                "Run `uv run python scripts/setup_login.py` to log in."
            )
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(storage_state=str(self.auth_file))
                page = await context.new_page()

                captured: asyncio.Future[str] = asyncio.Future()

                def on_request(req):
                    if captured.done() or self.api_host_substring not in req.url:
                        return
                    auth = req.headers.get("authorization")
                    if not (auth and auth.lower().startswith("bearer ")):
                        return
                    jwt = auth.split(" ", 1)[1]
                    if _is_b2c_token(jwt):
                        captured.set_result(jwt)

                page.on("request", on_request)
                await page.goto(self.trigger_url, wait_until="domcontentloaded")
                try:
                    jwt = await asyncio.wait_for(captured, timeout=30)
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        f"No authenticated request to {self.api_host_substring} "
                        f"appeared after loading {self.trigger_url} — the saved "
                        f"session in {self.auth_file} has likely expired. "
                        "Run `uv run python scripts/setup_login.py` to log in again."
                    ) from None

                # Persist freshened cookies/localStorage so the next mint
                # uses the latest MSAL refresh state.
                state = await context.storage_state()
                self.auth_file.write_text(json.dumps(state, indent=2))

                return CachedToken(jwt=jwt, expires_at=_decode_exp(jwt))
            finally:
                await browser.close()


class FallbackAuth:
    """Try each auth in order; skip ones that have failed this session.

    Exists because the wegmans.com (shop) token is accepted by the Meals2Go
    backend too — so when auth.json has expired but auth-shop.json is alive,
    Meals2Go tools can keep working on the shop token instead of failing.
    A failed mint costs a ~30s browser timeout, so failures are remembered
    and that source isn't retried for the rest of the session.
    """

    def __init__(self, *chain: WegmansAuth):
        self.chain = list(chain)
        self._dead: set[int] = set()

    async def get_token(self) -> str:
        last_err: Exception | None = None
        for i, auth in enumerate(self.chain):
            if i in self._dead:
                continue
            try:
                return await auth.get_token()
            except RuntimeError as e:
                self._dead.add(i)
                last_err = e
        raise last_err or RuntimeError(
            "No usable Wegmans auth. Run `uv run python scripts/setup_login.py`."
        )


def shop_auth(auth_file: Path = SHOP_AUTH_FILE) -> WegmansAuth:
    """Auth against the wegmans.com grocery site (commerce backend).

    Same silent-renewal strategy as Meals2Go, but the grocery MSAL client
    requests `offline_access`, so its cache holds a refresh token and renewal
    keeps working long after the 1-hour access token dies.
    """
    return WegmansAuth(
        auth_file=auth_file,
        trigger_url=SHOP_TRIGGER_URL,
        api_host_substring=SHOP_API_HOST_SUBSTRING,
    )
