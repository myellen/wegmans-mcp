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
- `src/wegmans_mcp/server.py` — FastMCP server exposing the MCP tools. Uses
  a module-level singleton `_client` so state (store, fulfillment)
  persists across tool calls in a single MCP session.
- `src/wegmans_mcp/login.py` — the interactive (headed-browser) login
  flow, shared by `scripts/setup_login.py` and the `setup_wegmans_login`
  MCP tool. Auto-installs Chromium if missing.
- `manifest.json` + `.mcpbignore` — Claude Desktop bundle (MCPB, uv
  server type). Build with `mcpb pack . dist/wegmans-mcp.mcpb`, then
  publish it as a GitHub release asset
  (`gh release create vX.Y.Z dist/wegmans-mcp.mcpb`) — the README's
  install link points at Releases, so a bundle change isn't shipped
  until a release carries it. The bundle stores auth under
  `~/.wegmans-mcp/` (set via env in the manifest) so sessions survive
  extension updates.

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

## Two separate storefronts

Meals2Go (prepared food) and wegmans.com (groceries) are different systems
with **separate carts**, and the tools are not interchangeable:

- **Prepared food** — `list_menu_categories` / `get_item_details` /
  `add_to_cart` / `view_cart`. Kit-based, auth required.
- **Grocery catalog** — `search_groceries` / `get_grocery_product`.
  SKU-based, backed by Algolia, **no auth required**.
- **Grocery cart** — `view_grocery_cart` / `add_grocery_to_cart` /
  `update_grocery_cart_item` / `remove_grocery_from_cart`. A commercetools
  cart on the commerce backend; needs a wegmans.com login (`auth-shop.json`).

`store_id` is shared across both (verified: 113 common IDs against the
Meals2Go location list), so `set_fulfillment` applies to grocery lookups too.
Grocery cart mutations auto-sync the server-side cart context (changestore)
to the client's store/fulfillment before writing.

Auth: `auth.json` (meals2go session) and `auth-shop.json` (wegmans.com
session) are both written by `scripts/setup_login.py`. The shop token is
accepted by both backends (shared audience, superset scopes), so the server
falls back to shop auth for everything when `auth.json` is missing. The
shop client gets refresh tokens (`offline_access`); Meals2Go does not.

Grocery gotchas:
- The Algolia field is **`fulfilmentType`** (one `l`) — Meals2Go's is
  `fulfillmentType` (two). A misspelling returns unfiltered results
  rather than erroring, so it silently reports items the store doesn't carry.
- Always keep `excludeFromWeb:false AND isSoldAtStore:true` in the filter.
- Pickup has no price block of its own; it bills at `price_inStore`.
  Delivery runs ~15% higher. Quote the channel the user is actually using.
- Cart mutations must re-GET the cart first — `cartVersion` is optimistic
  concurrency and the server bumps it several times per write.
- The server re-prices line items authoritatively (promos applied
  server-side), so quoted search prices can differ from cart prices.
- The in-store "My List" on wegmans.com IS the grocery cart
  (`fulfillmentType: instore`); there is no separate list object.

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
