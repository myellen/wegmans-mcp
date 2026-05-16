# wegmans-mcp — conventions for Claude

This is an MCP server that lets Claude manipulate a Wegmans Meals2Go cart.
Tools, auth, and API quirks are documented in `docs/api-discovery.md`.

## Architecture in 30 seconds

- `src/wegmans_mcp/auth.py` — mints Meals2Go bearer JWTs by replaying a
  saved logged-in browser session in headless Chromium (Playwright). The
  Meals2Go SPA does not request `offline_access`, so there is no refresh
  token; we use MSAL.js silent renewal in a fresh browser context.
- `src/wegmans_mcp/client.py` — async httpx client wrapping the
  `wegapi.azure-api.net` API surface (cart, kitting, locations, coupons,
  Google maps proxy). Holds the mutable `store_id` / `fulfillment_type`.
- `src/wegmans_mcp/server.py` — FastMCP server exposing 13 tools. Uses
  a module-level singleton `_client` so state (store, fulfillment)
  persists across tool calls in a single MCP session.

## Working with the cart API

- **Cart is keyed on customerId, not storeId.** Switching stores doesn't
  create a new cart — it just changes which store's availability/pricing
  context applies to the same items.
- **No SET endpoint for fulfillment.** `fulfillmentType` (`store` /
  `curbside` / `delivery`) and `storeId` are passed as URL/query params
  on each call. The `set_fulfillment` tool only updates in-process state.
- **Echo-the-kit payload.** Add/PATCH cart-items takes the **whole kit
  definition** (from `GET /kitting/.../kits/{kitId}`) as `payload`. The
  caller mutates `isSelected`/`selectedQuantity` on chosen entries.
  `build_add_payload()` in `client.py` does the mutation.
- **Truncate, don't flag.** For each `itemList` / `kitList` /
  `itemAttributeSets[*].attributes`, the server expects the list pruned
  to ONLY the chosen entries. Leaving unselected entries in causes the
  server to silently discard all selections and mark the item
  unavailable (no error response).
- **Three nesting levels** in subs/wraps:
  1. Top-level kit (e.g., kit 458 Chicken Tender)
  2. Size group → `kitList` of sub-kits (Small/Medium/Large = own kit IDs)
  3. Each sub-kit has its own modifier groups, and each topping item may
     have an `itemAttributeSets` (Light / Regular / Extra / On Side)
- **`get_item_details` returns the full nested tree** in one call —
  every sub-kit, every option, plus `amount_options` on items that have
  Light/Regular/Extra. Always start from there before constructing
  `add_to_cart` selections; never probe kit IDs by guessing.
- **`add_to_cart` validates before POST.** If any required (min>0)
  group lacks a selection, the tool raises `ValueError` with a structured
  list of unsatisfied groups and their available options. Wegmans itself
  silently accepts under-configured items at $0 — validation prevents that.
- **PATCH with `quantity: 0` removes** an item. There is no DELETE.

## Code conventions

- Async everywhere; `httpx.AsyncClient`, `asyncio`. No sync paths.
- `WegmansClient` is also an async context manager — prefer
  `async with WegmansClient(auth) as c:` outside the long-lived server.
- The MCP server holds one persistent `_client` via `_get_client()`; do
  not recreate it per tool call.
- Selections dict format: `{kit_content_id: {entity_id: spec}}` where
  `entity_id` is an item_id OR a sub-kit_id and `spec` is an int
  (quantity) or `{"quantity": n, "attribute": "Extra"}`.
- Never log full JWTs or `auth.json` contents. Print payload claims
  selectively (iss, aud, azp, scp, exp — skip the signature).

## Capture-first when adding a new endpoint

Wegmans' API has subtle truncation/encoding rules that the docs don't
exist for. Before writing code against a new endpoint:

1. Drive the meals2go.com UI via the Playwright MCP to perform the
   action once.
2. Use `mcp__playwright__browser_network_request` to save the live
   request body + headers to `docs/captures/<name>.json`.
3. Diff your generated payload against the captured one before
   committing.

Past gotchas this caught:
- Wings cart-add failed silently when `itemList` wasn't truncated.
- Sub toppings registered as "all variants selected" until I started
  truncating `itemAttributeSets.attributes` to the chosen one.

## Memories

User-scoped memories under `~/.claude/projects/-home-max-wegmans-mcp/memory/`
hold persistent guidance:
- `wegmans_kit_modifiers.md` — topping attribute quirks and `madeFor`
- `wegmans_dev_tooling.md` — WSL2 Node/Playwright setup
- `project_goal.md`, `user_profile.md` — collaboration context
