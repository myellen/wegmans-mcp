"""One-time interactive Wegmans login (terminal flavor).

Opens a real browser window. Log in normally. The script:
  1. Waits until an Azure B2C token appears on wegapi.azure-api.net
     (i.e. you've successfully signed in).
  2. Auto-detects your Shoppers Club number from the digital-coupons
     request the home page fires, and writes it to a local .env file.
  3. Loads wegmans.com so the grocery site signs in off the same account.
  4. Saves the sessions to auth.json and (grocery) auth-shop.json.

Run: `uv run python scripts/setup_login.py`

The same flow is available from inside Claude Desktop via the
`setup_wegmans_login` MCP tool — this script is for terminal users.
All logic lives in wegmans_mcp.login.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wegmans_mcp.login import run_login  # noqa: E402

AUTH_FILE = Path("auth.json")
SHOP_AUTH_FILE = Path("auth-shop.json")
ENV_FILE = Path(".env")


def _print_status(state: str, detail: str) -> None:
    print(detail)


async def main() -> int:
    try:
        result = await run_login(
            auth_file=AUTH_FILE,
            shop_auth_file=SHOP_AUTH_FILE,
            env_file=ENV_FILE,
            on_status=_print_status,
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    if not result["shop_ok"]:
        print(
            "Grocery-site sign-in didn't complete; grocery cart tools may "
            "need another run. Catalog search will still work."
            + (f" Kept existing {SHOP_AUTH_FILE} untouched."
               if SHOP_AUTH_FILE.exists() else ""),
            file=sys.stderr,
        )
    if result["loyalty_id"]:
        print(f"Loyalty number saved to {ENV_FILE}.")
    else:
        print(
            "Couldn't detect your loyalty number automatically. "
            "Set WEGMANS_LOYALTY_ID manually if you want to use coupon tools.",
            file=sys.stderr,
        )
    print(f"Saved: {', '.join(result['saved'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
