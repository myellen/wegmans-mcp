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
    def __init__(self, auth_file: Path = DEFAULT_AUTH_FILE):
        self.auth_file = auth_file
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
                    if captured.done() or API_HOST_SUBSTRING not in req.url:
                        return
                    auth = req.headers.get("authorization")
                    if not (auth and auth.lower().startswith("bearer ")):
                        return
                    jwt = auth.split(" ", 1)[1]
                    if _is_b2c_token(jwt):
                        captured.set_result(jwt)

                page.on("request", on_request)
                await page.goto(TOKEN_TRIGGER_URL, wait_until="domcontentloaded")
                jwt = await asyncio.wait_for(captured, timeout=20)

                # Persist freshened cookies/localStorage so the next mint
                # uses the latest MSAL refresh state.
                state = await context.storage_state()
                self.auth_file.write_text(json.dumps(state, indent=2))

                return CachedToken(jwt=jwt, expires_at=_decode_exp(jwt))
            finally:
                await browser.close()
