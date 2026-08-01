# wegmans-mcp

Let Claude run your Wegmans Meals2Go cart. Ask Claude things like:

> "Add a large chicken tender sub on sesame to my cart at the Amherst St. store, all the free toppings, buffalo style."

> "Clip all my digital coupons."

> "What's in my cart?"

> "What does Wegmans charge for organic whole milk at the Amherst St. store?"

Claude (in Claude Code, Claude Desktop, or any MCP-compatible client) gets
15 tools for browsing the menu, finding stores, configuring fulfillment,
managing cart items, naming them ("for Dad", "for Akanksha"), clipping
Shoppers Club coupons, and searching the grocery catalog. It does **not**
place the final order — you still hit Checkout yourself.

**Prepared food vs. groceries.** Cart tools drive the *Meals2Go* cart
(subs, pizza, wings). The two grocery tools — `search_groceries` and
`get_grocery_product` — search the regular supermarket catalog for prices,
pack sizes, aisles, dietary tags, and nutrition. Grocery search needs no
login at all. There is **no grocery cart yet**: Claude can price out a
shopping list and tell you what Wegmans carries, but can't add those items
to an order.

## Setup (one-time, ~5 minutes)

You'll need:
- **Python 3.11+** — most macs and modern Linux already have it. `python3 --version` to check.
- **`uv`** — fast Python installer. Install with `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or see [uv docs](https://docs.astral.sh/uv/getting-started/installation/).
- **Claude Code** or another MCP client — [Claude Code docs](https://docs.claude.com/en/docs/claude-code/overview).
- **A Wegmans account** with at least one order or stored payment method.

Then:

```bash
git clone https://github.com/myellen/wegmans-mcp.git
cd wegmans-mcp
uv sync                              # creates a venv, installs deps
uv run playwright install chromium   # downloads the browser
uv run python scripts/setup_login.py # a real browser window opens — sign in to Wegmans
```

The setup script opens a real Chromium window. Click **Sign In** (top right on meals2go.com) and complete login. Once you're back at the home page logged in, the script:

- Saves your session to `auth.json`.
- **Auto-detects your Shoppers Club loyalty number** by sniffing the digital-coupons request the home page fires, and writes it to a `.env` file. The MCP server loads it on startup — you don't need to look up your loyalty number to use `list_coupons` / `clip_coupons`.

Both files are gitignored. Treat them like passwords — `auth.json` is the equivalent of being logged in on a browser.

## Wiring it into Claude Code

```bash
claude mcp add wegmans --scope user -- \
    uv --directory /full/path/to/wegmans-mcp run wegmans-mcp
```

(Replace the path with where you cloned the repo.) `--scope user` makes it available in every project; use `--scope local` to limit it to one project.

Restart Claude Code. The tools should appear under `mcp__wegmans__*` in the toolbox.

### Wiring it into Claude Desktop

Add this block to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "wegmans": {
      "command": "uv",
      "args": ["--directory", "/full/path/to/wegmans-mcp", "run", "wegmans-mcp"]
    }
  }
}
```

Restart Claude Desktop.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `WEGMANS_AUTH_FILE` | `auth.json` (in the working dir) | Path to the saved login state |
| `WEGMANS_STORE_ID` | `91` (Amherst St., Buffalo NY) | Default Meals2Go store number — you can override anytime via `set_fulfillment` |
| `WEGMANS_LOYALTY_ID` | auto-detected by `setup_login.py` and saved to `.env` | Your Shoppers Club number. Set this manually only if auto-detection fails or you want to override. |

To pass env vars when adding to Claude Code:

```bash
claude mcp add wegmans --scope user \
    -e WEGMANS_LOYALTY_ID=1234567 \
    -e WEGMANS_STORE_ID=16 \
    -- uv --directory /path/to/wegmans-mcp run wegmans-mcp
```

## What it can do

**Cart:**
- `view_cart` — see items, totals, current store/fulfillment, selected modifiers
- `add_to_cart` — add an item with chosen modifiers (and optionally a name)
- `update_cart_item_quantity` — change quantity (0 to remove)
- `remove_from_cart` — convenience for quantity=0
- `set_cart_item_name` — label an item ("for Akanksha")

**Menu:**
- `list_menu_categories` — top-level Meals2Go sections (Pizza & Wings, Subs, Bowls, ...)
- `browse_category` — drill into a category
- `get_item_details` — full item info with required modifiers

**Fulfillment + stores:**
- `search_stores` — find Wegmans locations (sort by distance to a city/zip)
- `get_current_fulfillment` — see active store + fulfillment
- `set_fulfillment` — switch store and/or pickup type (Carryout / Curbside / Delivery)

**Digital coupons:**
- `list_coupons` — list digital coupons with clipped status. Two sources: `"shop"` (default — the main wegmans.com Shoppers Club coupons, 100+) and `"meals2go"` (smaller set tied to Meals2Go orders, requires `WEGMANS_LOYALTY_ID`)
- `clip_coupons` — clip specific offers or all unclipped at once. Same `source` parameter as `list_coupons`

## Example prompts

Once the server is wired in, you can say things like:

- "Switch my order to curbside at the Amherst St. Wegmans."
- "Add a large chicken tender sub on sesame, buffalo style, all the free toppings, extra hot banana peppers. Call it 'for Max'."
- "What stores within 10 miles of 22030 offer delivery?"
- "Clip all my coupons."
- "What's in my cart and what's the total?"
- "Remove the wrap."

## Privacy

`wegmans-mcp` runs entirely on your own machine. Nothing leaves your computer except calls to Wegmans' own API (the same endpoints meals2go.com uses). Your saved session in `auth.json` is the equivalent of being logged in on a browser — keep it private.

If you stop using it: `rm auth.json` and `claude mcp remove wegmans`.

## Troubleshooting

**"Auth file ... missing"** — run `uv run python scripts/setup_login.py` again.

**"Item is unavailable" after add** — likely a missing required modifier. Call `get_item_details(kit_id)` first to see which `kit_content_id`s have `min_quantity > 0`. For subs and wraps, the size is selected via the SIZE group's sub-kits (Small/Medium/Large = their own kit_ids).

**Loyalty tools fail** — set `WEGMANS_LOYALTY_ID` to your Shoppers Club number.

**Token-mint timeout** — your session expired. `rm auth.json` and re-run `setup_login.py`.

## Bundled Claude Skill

Inside `.claude/skills/wegmans/` is an [Agent
Skill](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
that auto-loads when a user mentions Wegmans / Meals2Go in a Claude
session. It carries the non-obvious knowledge we've discovered — the
menu's nested kit structure, the topping Light/Regular/Extra
attribute, the "all free toppings + spiciest else" idiom, and
onboarding instructions for new users.

Claude Code working in this repo picks the skill up automatically. To
make it available to every project on your machine, copy or symlink it:

```bash
# user-level install
cp -r .claude/skills/wegmans ~/.claude/skills/
```

The skill *complements* the MCP server — it doesn't replace it. The
MCP server still does the actual API work; the skill helps Claude use
it well.

## Project layout

- `src/wegmans_mcp/` — the package (auth, client, server)
- `scripts/setup_login.py` — one-time interactive login
- `.claude/skills/wegmans/` — the bundled Claude Agent Skill
- `docs/api-discovery.md` — reverse-engineered API notes
- `docs/captures/` — raw HTTP request/response samples for reference
- `CLAUDE.md` — conventions for AI-assisted development of this codebase
