# First-time onboarding for wegmans-mcp

Walk the user through these steps in order. Don't run anything that
requires interaction (the browser login) silently — make sure they're
watching.

## Shortcut: Claude Desktop bundle

If the user is in the **Claude Desktop app**, skip the manual steps:
have them download `wegmans-mcp.mcpb` from the repo's GitHub releases
and open it (Settings → Extensions). Once installed, the login happens
in-chat: call `setup_wegmans_login`, tell the user to sign in when the
browser window appears, and poll `check_login_status` until it reports
done. Then jump to step 5 (set their home store). The manual path below
is for Claude Code / other MCP clients.

## 1. Prerequisites

Check that the user has these. If not, point them at the install links
and stop until they've installed them.

- **Python 3.11+** — `python3 --version`. Most macOS / modern Linux
  already have it. Windows users typically need to install from
  python.org or run inside WSL.
- **`uv`** — `which uv`. Install with
  `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS/Linux.
- An MCP-capable client — **Claude Desktop** (recommended) or **Claude
  Code**.
- A **Wegmans account** with at least one prior order or saved payment
  method (so login isn't a brand-new flow).

## 2. Clone and install

```bash
git clone https://github.com/myellen/wegmans-mcp.git
cd wegmans-mcp
uv sync
uv run playwright install chromium
```

## 3. One-time login

```bash
uv run python scripts/setup_login.py
```

This opens a real Chromium window. Have the user click **Sign In** at
the top of meals2go.com and complete the flow. Once they're back on
the home page, the script:

1. saves the Meals2Go session to `auth.json`,
2. **auto-detects the Shoppers Club loyalty number**, writing it to a
   local `.env` file,
3. then loads wegmans.com so the grocery site signs in off the same
   Wegmans account, and saves that session to `auth-shop.json` —
   this file is what the grocery **cart** tools need (grocery *search*
   needs no login at all).

If auto-detect of the loyalty number fails (rare), have them set
`WEGMANS_LOYALTY_ID` manually in the `.env`. If the wegmans.com step
prints a warning, grocery cart tools won't work — re-run the script.

`auth.json`, `auth-shop.json`, and `.env` are gitignored — treat them
like passwords.

## 4. Wire into the MCP client

### Claude Desktop

Edit `claude_desktop_config.json`:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- WSL development, Claude Desktop on Windows: see the Windows path
  above, and use `wsl` as the `command` with absolute paths inside:

  ```json
  "wegmans": {
    "command": "wsl",
    "args": ["/home/<user>/.local/bin/uv", "--directory",
             "/home/<user>/wegmans-mcp", "run", "wegmans-mcp"]
  }
  ```

Native install (macOS/Linux/Windows-native Python):

```json
{
  "mcpServers": {
    "wegmans": {
      "command": "uv",
      "args": ["--directory", "/full/path/to/wegmans-mcp",
               "run", "wegmans-mcp"]
    }
  }
}
```

Restart Claude Desktop after editing.

### Claude Code

```bash
claude mcp add wegmans --scope user -- \
  uv --directory /full/path/to/wegmans-mcp run wegmans-mcp
```

Use `--scope local` to limit to one project. After the command, the
tools appear in the next session.

## 5. Set their home store

The server defaults to store 91 (Amherst St., Buffalo NY). Unless the
user actually shops there, have them say something like "find my
Wegmans near <their city/zip> and make it my store" — that runs
`search_stores` + `set_fulfillment`. To make it permanent, set
`WEGMANS_STORE_ID=<store_id>` in the `.env` or MCP config so every
session starts there. Prices and availability are per-store, so skip
this and everything will quote the wrong store.

## 6. Sanity check

Two quick checks:

- "What's in my Wegmans cart?" → uses `mcp__wegmans__view_cart`
  (Meals2Go; often empty for a new user) and reports store + fulfillment.
- "Search Wegmans for bananas" → uses `mcp__wegmans__search_groceries`
  and returns priced results. This works even if login failed, so if
  search works but `view_grocery_cart` errors, the problem is
  `auth-shop.json` — re-run the login script.

If Claude says the tools aren't available at all, the MCP server didn't
load — see `troubleshooting.md`.
