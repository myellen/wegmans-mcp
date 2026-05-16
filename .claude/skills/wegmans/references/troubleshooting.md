# Troubleshooting wegmans-mcp

## Tools don't appear at all

The MCP server isn't loaded. Cause is usually one of:

- **Path mismatch** in `claude_desktop_config.json` — make sure the
  `--directory` arg points to the actual cloned repo path.
- **uv not on PATH** when Claude Desktop spawned. Use the absolute path
  to `uv` (e.g. `/Users/<you>/.local/bin/uv`) in the `command` field.
- **WSL crossing** — if the repo is in WSL but Claude Desktop is on
  Windows, the `command` must be `wsl` with absolute paths inside.
- **Claude Desktop wasn't restarted** after editing the config.

Try `claude mcp list` (Claude Code) to verify health. Or open Claude
Desktop's developer console (Cmd/Ctrl+Shift+I) and look at the MCP log.

## "Auth file ... missing" / token errors

Run `uv run python scripts/setup_login.py` again from the repo root.
If that hangs at "Waiting for an Azure B2C token", the user didn't
click "Sign In" — the script needs an actual login event to fire, not
just page load.

If the existing `auth.json` is stale (cookies expired), delete it and
re-run setup.

## "Item is unavailable" right after `add_to_cart`

A required modifier group was missing or the item is genuinely
unavailable at the chosen store. Steps:

1. Re-run `get_item_details(kit_id)`. Look at `modifier_groups` —
   every group with `min_quantity > 0` needs an entry in `selections`.
2. If the item has `sub_kits` (Size group on subs/wraps), one of those
   sub-kit IDs must be selected, AND its own required modifier_groups
   must also be filled in (same flat `selections` dict).
3. If everything is filled but the cart returns `is_available: false`,
   the store may not stock the item. Try `set_fulfillment(store_id=<x>)`
   on a different store and retry.

The validator that lives in `add_to_cart` should catch most of these
before they reach the wire — read its error message carefully; it
lists the missing groups by name.

## "Missing required selections for kit_id=..."

The validator did its job. The error message has the kit_content_id,
the prompt ("Choose bread", etc.), and the available items/sub_kits
to pick from. Add those to `selections` and try again. If a sub-kit
appears in `available_sub_kits` (e.g. Large size on a sub), selecting
that sub-kit pulls in **its** nested required groups — you'll likely
need to do two validator passes to catch them all, or just fill in
everything from `get_item_details` up front.

## Loyalty number not detected after `setup_login.py`

The setup script sniffs the `/digital-coupons/.../loyalty/<id>` URL
that fires on home-page load. If the user navigated away too quickly,
or if their account has no loyalty number on file (rare), it won't
fire.

Workaround: set `WEGMANS_LOYALTY_ID` manually in `.env`:

```
WEGMANS_LOYALTY_ID=1234567
```

The number is on the Wegmans card (the long digit string) or in
account settings on wegmans.com.

This only affects `clip_coupons(source="meals2go")` and
`list_coupons(source="meals2go")`. Shop-side coupons (the default)
don't need it.

## "I don't see the price update in my Wegmans browser"

The MCP server changes server-side state. The Wegmans browser tab is
showing cached data. **Refresh it.** That goes for cart contents,
clipped coupons, fulfillment changes — everything.

## ChatGPT can't connect

`wegmans-mcp` ships as a stdio MCP server. ChatGPT only accepts
remote HTTPS MCP servers. To make this work for ChatGPT you'd need to:

1. Add HTTP/SSE transport to the server (FastMCP supports it).
2. Tunnel it via Cloudflare Tunnel / ngrok / Tailscale Funnel.
3. Add a Bearer-token gate at the HTTP layer.
4. Use a ChatGPT Plus+ plan; note Business/Enterprise/Edu is required
   for write actions (add_to_cart, clip_coupons).

This isn't built in yet. Use Claude Desktop or Claude Code instead for
the smooth path.
