---
name: wegmans
description: Place, modify, and inspect Wegmans Meals2Go orders, search the Wegmans grocery catalog, build the wegmans.com grocery cart / shopping list, and clip Wegmans digital coupons via the wegmans-mcp MCP server. Use when the user mentions Wegmans, Meals2Go, ordering a sub/wrap/pizza/bowl from Wegmans, "my cart" or "my list" in a Wegmans context, Shoppers Club / digital coupons, switching pickup type (Carryout / Curbside / Delivery), finding a specific Wegmans store, or pricing/buying grocery items at Wegmans (including converting a shopping list from another chain into a Wegmans cart). Includes recipes for the menu's nested kit structure, the topping Light/Regular/Extra attribute, and onboarding a new user who doesn't have the MCP server installed yet. Do NOT use for recipe ideas or other supermarket chains.
---

# Wegmans Meals2Go via wegmans-mcp

The `wegmans-mcp` MCP server (https://github.com/myellen/wegmans-mcp) exposes
tools under `mcp__wegmans__*` for the Wegmans Meals2Go API, the grocery
catalog and cart, plus Shoppers Club coupons. This skill captures the non-obvious
knowledge you need to use them well — the menu's nesting quirks, the topping
attribute encoding, and how to onboard a new user.

## Two storefronts, two carts — don't mix them up

| You want | Use | Auth |
|---|---|---|
| Subs, pizza, wings, bowls | `list_menu_categories`, `get_item_details`, `add_to_cart`, `view_cart` | required |
| Find/price groceries | `search_groceries`, `get_grocery_product` | **none** |
| Build a grocery cart / list | `view_grocery_cart`, `add_grocery_to_cart`, `update_grocery_cart_item`, `remove_grocery_from_cart` | required (wegmans.com login) |

`add_to_cart` only takes Meals2Go kits; `add_grocery_to_cart` only takes
grocery SKUs. The two carts are separate systems and check out separately.

The grocery cart doubles as the wegmans.com/app "My List" when the
fulfillment type is in-store — items you add show up on the user's phone
sorted by aisle. Neither cart places the final order; the user hits
Checkout themselves.

Grocery search works even when auth is broken or the user has never logged
in, so it's the right fallback when cart tools fail. Cart prices are
recomputed server-side (promos apply), so the cart's line total can come
back *lower* than the search price — mention savings, don't "correct" them.

## Before doing anything

Check that the MCP tools are actually available. The signature ones are
`mcp__wegmans__view_cart`, `mcp__wegmans__get_item_details`, etc.

- **Tools available** → proceed using them.
- **Tools missing** → the user hasn't installed the MCP server yet.
  Walk them through `references/onboarding.md` before trying to take
  any action. Don't pretend an order succeeded if the server isn't there.

## How the menu is shaped (this surprises every fresh LLM call)

Wegmans menus are recursive. There are **three layers** and Claude
sessions repeatedly get stuck if they don't realize this:

1. **Top-level kit** — e.g. `kit_id 458 "Chicken Tender"`. Has
   modifier groups, but its Size group's `options` is **empty**
   because sizes are themselves kits.
2. **Sub-kits** in the Size group's `sub_kits` list — e.g. Small
   `kit_id 411` ($6.99), Medium `kit_id 459` ($9.99), Large
   `kit_id 408` ($15.99). Each sub-kit has its **own** modifier_groups
   (bread, style, toppings, …).
3. **Topping attribute sets** on items in the sub-kit's modifier
   groups — `amount_options: {codes: ["Light","Regular","Extra"], default: "Regular"}`.

`get_item_details(kit_id)` returns this entire tree in one call. **Always
call it before `add_to_cart`** and traverse the whole structure to find
the IDs you'll pass in `selections`. Never guess kit_ids; never probe
nearby numbers.

## Constructing `add_to_cart` selections

`selections` is a flat dict: `{kit_content_id: {entity_id: spec}}`.

- `entity_id` may be an `item_id` (from a modifier group's `options`)
  OR a `kit_id` (from a `sub_kits` list).
- `spec` is usually an int (quantity). For toppings with
  `amount_options`, use `{"quantity": 1, "attribute": "Extra"}` to pick
  Light/Regular/Extra/On Side. Omit `attribute` to get the default
  (almost always Regular).
- The picks for a sub-kit's own modifier groups go in the **same flat
  dict** at the same level — there is no nested dict.

Example skeleton for the Chicken Tender Sub, Large, Sesame, Buffalo:

```python
selections = {
    884:  {408: 1},          # Size: Large (sub-kit id 408)
    672:  {1022: 1},          # Choose bread: Sesame
    723:  {1076: 1},          # Choose style: Buffalo
    669:  {4133: 1},          # Toasting: default
    778:  {1580: 1},          # Slicing: default
    # toppings/condiments/cheese as desired
}
```

`add_to_cart` runs **pre-flight validation** — if any required
(min_quantity > 0) modifier group is unsatisfied, it raises with a
structured error listing what's missing. Trust the validator; don't
hand-roll your own.

## Common idioms users say

- **"All the free toppings, spiciest of everything else."**
  Means: for every multi-select modifier group (toppings, condiments,
  sauces), pick every option whose `price` is 0; respecting `max_quantity`.
  For every required single-select group (style, cheese, etc.), pick the
  spiciest option (Buffalo > Spicy > Hot variants > everything else;
  Pepper Jack cheese over Provolone; etc.). Bread/toasting/slicing have
  no spicy dimension — use the user's choice or the default.

- **"Make it for X."**
  Set `made_for="X"` on the relevant `add_to_cart` call. The cart UI
  shows it as "Who is this for?". Use the existing `set_cart_item_name`
  tool to label items after the fact.

- **"Clip all my coupons."**
  Call `clip_coupons()` with no args — defaults to `source="shop"`
  (the 100+ wegmans.com Shoppers Club set). Pass `source="meals2go"`
  for the smaller Meals2Go-tied set; that one also requires
  `WEGMANS_LOYALTY_ID` to be set in the user's env.

- **"Switch to <store name>."** or **"…near <zip>."**
  Call `search_stores(near="<zip or city>")`, find the right
  `store_id`, then `set_fulfillment(store_id=<id>, fulfillment_type="carryout|curbside|delivery")`.
  For **"make this my store"** / "I shop there now", add `remember=True`
  so it persists across sessions, and relay the `remembered.warning` if
  `effective_next_session` comes back false.

## Pitfalls to skip past

- **Don't add items without first calling `get_item_details`.**
  Submitting `add_to_cart` with an empty `selections` returns 200 from
  the server with an under-configured cart item at $0 — but our
  validator will catch this and refuse. Don't bypass it.

- **Don't include unselected options in your `selections` map.**
  The server-side payload truncates `itemList` / `kitList` / attribute
  arrays to chosen entries only; `build_add_payload` handles this for
  you. Just list what you want chosen.

- **Don't try to call ChatGPT-style URL connectors.** This MCP server
  is stdio-only by default. If a family member is on ChatGPT instead of
  Claude, point them to the README's HTTP-transport guidance.

## Onboarding a new user

If `mcp__wegmans__*` tools aren't available, the user hasn't installed
the server. Walk them through `references/onboarding.md` — it covers
clone + `uv sync` + Playwright install + interactive login + Claude
Desktop / Claude Code wiring. Don't skip the loyalty-number question;
the shop coupons don't need it but the Meals2Go coupons do.

## Reference files

- `references/onboarding.md` — first-time install walkthrough
- `references/recipes.md` — worked examples for common orders
- `references/troubleshooting.md` — what to do when something fails
