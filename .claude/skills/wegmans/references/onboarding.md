# First-time onboarding for wegmans-mcp

Walk the user through these steps in order. Don't run anything that
requires interaction (the browser login) silently — make sure they're
watching.

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
the home page, the script saves `auth.json` and **auto-detects the
Shoppers Club loyalty number**, writing it to a local `.env` file.

If auto-detect fails (rare — only if the home page doesn't fire the
digital-coupons request promptly), have them set `WEGMANS_LOYALTY_ID`
manually in the `.env`. The number is on their Wegmans card or in
account settings.

`auth.json` and `.env` are gitignored — treat them like passwords.

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

## 5. Sanity check

Ask Claude: "What's in my Wegmans cart?" The expected response uses
`mcp__wegmans__view_cart` and reports cart contents (often empty for a
new user) with the current store + fulfillment.

If Claude says the tools aren't available, the MCP server didn't load
— see `troubleshooting.md`.
