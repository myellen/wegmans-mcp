"""MCP server exposing Wegmans Meals2Go cart operations as tools."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .auth import SHOP_AUTH_FILE, FallbackAuth, WegmansAuth, shop_auth
from .client import GROCERY_PRICE_FIELDS, WegmansClient, normalize_fulfillment

# Load .env from the current working directory if present. setup_login.py
# writes the auto-discovered loyalty number there so the user doesn't have
# to set WEGMANS_LOYALTY_ID by hand. When auth lives in a dedicated
# directory (the Claude Desktop bundle sets WEGMANS_AUTH_FILE to
# ~/.wegmans-mcp/auth.json), the in-chat login writes .env next to it —
# load that one too.
# The Desktop bundle manifest injects user_config values as env vars even
# when the user left them blank. python-dotenv never fills a key that
# already exists — even as "" — so blank placeholders would permanently
# shadow the loyalty number the login saves to .env. Treat empty as unset.
for _k in ("WEGMANS_LOYALTY_ID", "WEGMANS_STORE_ID", "WEGMANS_FULFILLMENT_TYPE"):
    if os.environ.get(_k) == "":
        del os.environ[_k]

load_dotenv()
if os.environ.get("WEGMANS_AUTH_FILE"):
    load_dotenv(Path(os.environ["WEGMANS_AUTH_FILE"]).parent / ".env")

mcp = FastMCP(
    "wegmans-mcp",
    # Sent to the client on connect. Without it, a fresh Claude session has
    # to infer all of this — and reliably gets the two carts and the login
    # story wrong on the first try.
    instructions=(
        "Wegmans tools. This server runs LOCALLY on the user's own machine "
        "and drives their real Wegmans account.\n\n"
        "TWO SEPARATE CARTS — they are different systems and check out "
        "separately:\n"
        "  * Groceries (wegmans.com): search_groceries / get_grocery_product "
        "to find items; view_grocery_cart / add_grocery_to_cart / "
        "update_grocery_cart_item / remove_grocery_from_cart to manage the "
        "cart. When fulfillment is in-store this cart IS the user's 'My "
        "List' in the Wegmans app, sorted by aisle.\n"
        "  * Meals2Go (prepared food — subs, pizza, wings): "
        "list_menu_categories / browse_category / get_item_details, then "
        "add_to_cart / view_cart. Always call get_item_details before "
        "add_to_cart; menu items are deeply nested kits.\n\n"
        "AUTH: only the grocery *catalog* (search_groceries, "
        "get_grocery_product) works without login; everything else needs a "
        "saved session. If a tool reports an expired or missing session, "
        "call setup_wegmans_login — it opens a browser window on the user's "
        "machine where THEY sign in (never ask for their password), then "
        "poll check_login_status. Sessions expire every few weeks, so this "
        "is normal maintenance, not a broken install.\n\n"
        "NOTE: an empty Meals2Go cart always reports customer_id=null and an "
        "all-zeros cart_id. That is how the API represents an empty cart — "
        "it is NOT evidence of a failed login. Use check_login_status to "
        "judge auth state.\n\n"
        "Prices and availability are per store and per fulfillment type "
        "(delivery runs ~15% above in-store). Confirm the store with "
        "get_current_fulfillment before quoting totals. This server never "
        "places an order — the user always checks out themselves."
    ),
)

_auth: WegmansAuth | None = None
_client: WegmansClient | None = None


def _auth_paths() -> tuple[Path, Path]:
    auth_path = Path(os.environ.get("WEGMANS_AUTH_FILE", "auth.json"))
    shop_path = Path(os.environ.get("WEGMANS_SHOP_AUTH_FILE", str(SHOP_AUTH_FILE)))
    return auth_path, shop_path


def _get_client() -> WegmansClient:
    global _auth, _client
    if _client is None:
        auth_path, shop_auth_path = _auth_paths()
        store_id_env = os.environ.get("WEGMANS_STORE_ID")

        shop = shop_auth(shop_auth_path) if shop_auth_path.exists() else None
        # The shop token is accepted by the Meals2Go backend too (shared
        # audience, superset scopes — verified 2026-08-01), so a wegmans.com
        # login can carry the whole server when auth.json is missing or its
        # saved session has expired.
        meals = WegmansAuth(auth_file=auth_path) if auth_path.exists() else None
        if meals is not None and shop is not None:
            _auth = FallbackAuth(meals, shop)
        else:
            # Exactly one (or neither) exists; a missing-file WegmansAuth
            # raises a helpful setup message on first use.
            _auth = meals or shop or WegmansAuth(auth_file=auth_path)

        kwargs: dict = {"shop_auth": shop}
        if store_id_env:
            kwargs["store_id"] = int(store_id_env)
        fulfillment_env = os.environ.get("WEGMANS_FULFILLMENT_TYPE")
        if fulfillment_env:
            kwargs["fulfillment_type"] = normalize_fulfillment(fulfillment_env)
        _client = WegmansClient(_auth, **kwargs)
    return _client


def _loyalty_id() -> str:
    v = os.environ.get("WEGMANS_LOYALTY_ID")
    if not v:
        raise RuntimeError(
            "WEGMANS_LOYALTY_ID is not set. Set it to your Wegmans loyalty/Shoppers Club "
            "number (visible on your card or in your account profile)."
        )
    return v


def _summarize_cart(cart: dict[str, Any]) -> dict[str, Any]:
    items = []
    for ci in cart.get("cartItems") or []:
        payload = ci.get("payload") or {}
        chosen = []
        for section in payload.get("uiNavigationSections") or []:
            for kc in section.get("kitContents") or []:
                # Server-side cart truncates itemList to only the chosen
                # options, so every entry here counts as selected.
                for it in kc.get("itemList") or []:
                    chosen.append({
                        "group": kc.get("kitContentCopyHeader"),
                        "name": it.get("copyHeader"),
                        "quantity": it.get("selectedQuantity") or 1,
                        "item_id": it.get("itemId"),
                    })
        items.append({
            "cart_item_id": ci.get("cartItemId"),
            "name": payload.get("copyHeader"),
            "kit_id": payload.get("kitId"),
            "quantity": ci.get("quantity"),
            "unit_price": ci.get("unitPrice"),
            "is_available": ci.get("isAvailable"),
            "note": ci.get("note"),
            "made_for": ci.get("madeFor"),
            "selected_options": chosen,
        })
    return {
        "cart_id": cart.get("cartId"),
        "customer_id": cart.get("customerId"),
        "cart_type": cart.get("cartType"),
        "mode": cart.get("mode"),
        "food_total": cart.get("foodTotal"),
        "sub_total": cart.get("subTotal"),
        "total_taxes": cart.get("totalTaxes"),
        "total_price": cart.get("totalPrice"),
        "status": (cart.get("status") or {}).get("cartStatusCopyText"),
        "can_checkout": (cart.get("status") or {}).get("canCheckout"),
        "checkout_requirements": (cart.get("status") or {}).get("checkoutRequirementsCopyText"),
        "items": items,
    }


def _price(hit: dict[str, Any], channel: str) -> dict[str, Any] | None:
    """Pull the price block matching the channel, falling back to in-store."""
    field = GROCERY_PRICE_FIELDS.get(channel, "price_inStore")
    block = hit.get(field) or hit.get("price_inStore") or {}
    if not block:
        return None
    return {"amount": block.get("amount"), "unit_price": block.get("unitPrice")}


def _summarize_product(hit: dict[str, Any], channel: str = "instore") -> dict[str, Any]:
    return {
        "sku_id": hit.get("skuId"),
        "name": hit.get("productName"),
        "brand": hit.get("consumerBrandName"),
        "pack_size": hit.get("packSize"),
        "price": _price(hit, channel),
        "sold_by_weight": hit.get("isSoldByWeight"),
        "sell_by_unit": hit.get("onlineSellByUnit"),
        "max_quantity": hit.get("maxQuantity"),
        "is_available": hit.get("isAvailable"),
        "category": (hit.get("categoryNodes") or {}).get("lvl2")
        or (hit.get("categoryNodes") or {}).get("lvl0"),
        "aisle": (hit.get("planogram") or {}).get("aisle"),
        "tags": hit.get("filterTags") or [],
        "has_offers": hit.get("hasOffers"),
        "coupon_offer_ids": hit.get("digitalCouponsOfferIds") or [],
        "rating": hit.get("averageStarRating"),
        "review_count": hit.get("reviewCount"),
        "url": f"https://www.wegmans.com/shop/product/{hit['slug']}"
        if hit.get("slug")
        else None,
    }


def _flatten_kits(node: dict[str, Any], path: list[str], out: list[dict[str, Any]]) -> None:
    """Walk a menuContent subtree and collect leaf orderable kits."""
    for child in node.get("menuContents") or []:
        name = child.get("copyHeader") or ""
        if child.get("contentType") == "Category":
            # Categories have children-only metadata here; the menu fetcher
            # handles recursion via separate API calls.
            continue
        # Non-Category nodes are referenceable kits via commerceItemNumber/contentId
        out.append({
            "name": name,
            "path": " / ".join(path + [name]) if name else " / ".join(path),
            "content_id": child.get("contentId"),
            "menu_content_id": child.get("menuContentId"),
            "commerce_item_number": child.get("commerceItemNumber"),
            "price_range": child.get("menuPriceRange"),
            "is_available": child.get("isFulfillmentAvailable"),
            "is_promo": child.get("isPromo"),
        })


@mcp.tool()
async def view_cart() -> dict[str, Any]:
    """Get the current Meals2Go cart with all items, modifiers, and totals."""
    client = _get_client()
    cart = await client.get_cart()
    out = _summarize_cart(cart)
    out["fulfillment_type"] = client.fulfillment_type
    out["store_id"] = client.store_id
    return out


@mcp.tool()
async def get_current_fulfillment() -> dict[str, Any]:
    """Show the current store_id and fulfillment_type the cart is operating against."""
    client = _get_client()
    return {"store_id": client.store_id, "fulfillment_type": client.fulfillment_type}


@mcp.tool()
async def set_fulfillment(
    store_id: Annotated[int, Field(description="store_id from search_stores")],
    fulfillment_type: Annotated[
        str,
        Field(description="One of: 'store' (Carryout), 'curbside', 'delivery'. 'carryout' is accepted as an alias for 'store'."),
    ],
) -> dict[str, Any]:
    """Set the store + fulfillment context used by subsequent cart operations.

    Wegmans does not have a server-side SET — this just changes what the MCP
    server passes to the cart endpoints. It does NOT modify the customer's
    persisted preference in the web/mobile app.
    """
    client = _get_client()
    new_state = client.set_fulfillment(store_id, fulfillment_type)
    cart = await client.get_cart()
    return {"applied": new_state, "cart": _summarize_cart(cart)}


@mcp.tool()
async def search_stores(
    near: Annotated[
        str | None,
        Field(description="Optional city/zip/state to sort by distance, e.g. 'Fairfax, VA' or '22030'"),
    ] = None,
    fulfillment_type: Annotated[
        str | None,
        Field(description="Optional filter: 'store', 'curbside', or 'delivery'"),
    ] = None,
    max_results: Annotated[int, Field(description="How many stores to return", ge=1, le=50)] = 10,
) -> list[dict[str, Any]]:
    """Search Wegmans Meals2Go store locations. Pass `near` to sort by distance."""
    client = _get_client()
    return await client.search_stores(near=near, fulfillment_type=fulfillment_type, max_results=max_results)


@mcp.tool()
async def list_menu_categories() -> list[dict[str, Any]]:
    """List the top-level Meals2Go menu categories (Pizza & Wings, Bowls, Subs, ...)."""
    client = _get_client()
    menu = await client.get_menu()
    cats = []
    for m in menu.get("menus") or []:
        for c in m.get("menuContents") or []:
            cats.append({
                "name": c.get("copyHeader"),
                "menu_id": m.get("menuId"),
                "menu_content_id": c.get("menuContentId"),
                "content_id": c.get("contentId"),
                "is_available": c.get("isFulfillmentAvailable"),
            })
    return cats


_KIT_HREF_RE = re.compile(r"/kits/(\d+)")
_ITEM_HREF_RE = re.compile(r"/items/(\d+)")


def _extract_kit_or_item_id(links: list | None) -> dict[str, int]:
    """Walk the HAL `links` array and pull out kit_id / item_id when present."""
    out: dict[str, int] = {}
    for ln in links or []:
        href = ln.get("href") or ""
        if ln.get("rel") == "kit":
            m = _KIT_HREF_RE.search(href)
            if m:
                out["kit_id"] = int(m.group(1))
        elif ln.get("rel") == "item":
            m = _ITEM_HREF_RE.search(href)
            if m:
                out["item_id"] = int(m.group(1))
    return out


@mcp.tool()
async def browse_category(
    menu_id: Annotated[int, Field(description="Menu ID (typically 1)")],
    menu_content_id: Annotated[int, Field(description="menuContentId of the category")],
) -> dict[str, Any]:
    """Browse one level deeper into a menu category. Returns the children nodes.

    For leaf entries the response includes `content_type` ("Kit" / "Item"),
    plus `kit_id` (when the entry is a configurable kit you can pass to
    get_item_details / add_to_cart) or `item_id`. Categories carry only
    `menu_content_id` — pass that back into browse_category to drill in.
    """
    client = _get_client()
    node = await client.get_menu_children(menu_id, menu_content_id)
    items: list[dict[str, Any]] = []
    sub_cats: list[dict[str, Any]] = []
    for c in node.get("menuContents") or []:
        rec = {
            "name": c.get("copyHeader"),
            "content_type": c.get("contentType"),
            "menu_content_id": c.get("menuContentId"),
            "content_id": c.get("contentId"),
            "is_available": c.get("isFulfillmentAvailable"),
            "price_range": c.get("menuPriceRange"),
            "is_promo": c.get("isPromo"),
        }
        rec.update(_extract_kit_or_item_id(c.get("links")))
        if c.get("contentType") == "Category":
            sub_cats.append(rec)
        else:
            items.append(rec)
    return {
        "menu_content_id": node.get("menuContentId"),
        "sub_categories": sub_cats,
        "items": items,
    }


def _shape_item(it: dict[str, Any]) -> dict[str, Any]:
    out = {
        "item_id": it.get("itemId"),
        "name": it.get("copyHeader"),
        "price": it.get("price"),
        "default_choice": it.get("defaultChoice"),
        "is_available": it.get("isFulfillmentAvailable"),
    }
    sets = it.get("itemAttributeSets") or []
    if sets:
        attrs = sets[0].get("attributes") or []
        codes = [a.get("code") for a in attrs if a.get("code")]
        default = next((a.get("code") for a in attrs if a.get("defaultAttribute")), None)
        out["amount_options"] = {"codes": codes, "default": default}
    return out


def _shape_group(section: dict[str, Any], kc: dict[str, Any]) -> dict[str, Any]:
    return {
        "kit_content_id": kc.get("kitContentId"),
        "section_code": section.get("sectionCode"),
        "prompt": kc.get("kitContentCopyHeader"),
        "min_quantity": kc.get("minimumOrderQuantity"),
        "max_quantity": kc.get("maximumOrderQuantity"),
        "allow_multiples": kc.get("allowMultiples"),
        "quantity_at_no_charge": kc.get("quantityAtNoCharge"),
        "options": [_shape_item(it) for it in (kc.get("itemList") or [])],
        "sub_kits": [_shape_sub_kit(sk) for sk in (kc.get("kitList") or [])],
    }


def _shape_kit(kit: dict[str, Any]) -> dict[str, Any]:
    return {
        "kit_id": kit.get("kitId"),
        "name": kit.get("copyHeader"),
        "description": kit.get("copyText"),
        "price": kit.get("price"),
        "pricing_method": kit.get("pricingMethod"),
        "product_type": kit.get("productType"),
        "is_available": kit.get("isFulfillmentAvailable"),
        "is_promo": kit.get("isPromo"),
        "modifier_groups": [
            _shape_group(s, kc)
            for s in (kit.get("uiNavigationSections") or [])
            for kc in (s.get("kitContents") or [])
        ],
    }


def _shape_sub_kit(sk: dict[str, Any]) -> dict[str, Any]:
    base = _shape_kit(sk)
    base["default"] = sk.get("defaultChoice", False)
    return base


@mcp.tool()
async def get_item_details(
    kit_id: Annotated[int, Field(description="kit_id of the orderable item")],
) -> dict[str, Any]:
    """Get full nested details of a menu item.

    Returns the **complete decision tree** the caller needs to plan an
    add_to_cart in one call. Each modifier_group has both `options`
    (leaf items from itemList) and `sub_kits` (kits from kitList — used
    for Size groups on subs/wraps, where Small/Medium/Large are
    themselves kits). Each sub-kit carries its own `modifier_groups`,
    recursively.

    For toppings/condiments/sauces with a Light/Regular/Extra picker,
    each leaf option includes an `amount_options` field listing the
    available codes (and which is the default). Pass the chosen code
    as the `attribute` field in `add_to_cart`'s selection spec.

    Use this before add_to_cart. You should not need any other call to
    enumerate available choices.
    """
    client = _get_client()
    kit = await client.get_kit(kit_id)
    return _shape_kit(kit)


def _spec_quantity(spec: Any) -> int:
    if isinstance(spec, int):
        return spec
    if isinstance(spec, dict):
        try:
            return int(spec.get("quantity", 1))
        except (TypeError, ValueError):
            return 1
    return 0


def _validate_selections(kit: dict[str, Any], selections: dict[int, dict[int, Any]]) -> list[dict[str, Any]]:
    """Walk the kit (recursing into selected sub-kits) and return a list of
    unsatisfied required modifier groups.

    A group is satisfied when its kitContentId has at least one entry in
    `selections` whose quantities sum to >= minimumOrderQuantity, and at
    least one entry's key matches an itemId in itemList or a kitId in
    kitList for that group.
    """
    problems: list[dict[str, Any]] = []

    def walk(sections: list, path: str) -> None:
        for sec in sections or []:
            for kc in sec.get("kitContents") or []:
                kc_id = kc.get("kitContentId")
                min_q = kc.get("minimumOrderQuantity") or 0
                chosen = selections.get(kc_id) or {}
                valid_item_ids = {it.get("itemId") for it in (kc.get("itemList") or [])}
                valid_kit_ids = {sk.get("kitId") for sk in (kc.get("kitList") or [])}
                matched = {k: v for k, v in chosen.items() if k in valid_item_ids or k in valid_kit_ids}
                total_qty = sum(_spec_quantity(v) for v in matched.values())

                if min_q > 0 and total_qty < min_q:
                    problems.append({
                        "kit_content_id": kc_id,
                        "prompt": kc.get("kitContentCopyHeader"),
                        "path": path,
                        "min_quantity": min_q,
                        "selected_quantity": total_qty,
                        "available_items": [
                            {"item_id": it.get("itemId"), "name": it.get("copyHeader")}
                            for it in (kc.get("itemList") or [])
                        ],
                        "available_sub_kits": [
                            {"kit_id": sk.get("kitId"), "name": sk.get("copyHeader")}
                            for sk in (kc.get("kitList") or [])
                        ],
                    })

                for sk in kc.get("kitList") or []:
                    if sk.get("kitId") in matched:
                        sub_path = f"{path} > {sk.get('copyHeader') or sk.get('kitId')}" if path else (sk.get("copyHeader") or str(sk.get("kitId")))
                        walk(sk.get("uiNavigationSections"), sub_path)

    walk(kit.get("uiNavigationSections"), kit.get("copyHeader") or str(kit.get("kitId")))
    return problems


def _format_validation_error(kit_id: int, problems: list[dict[str, Any]]) -> str:
    lines = [f"Missing required selections for kit_id={kit_id}:"]
    for p in problems:
        loc = f" (under {p['path']})" if p.get("path") else ""
        lines.append(
            f"  - kit_content_id {p['kit_content_id']} {p['prompt']!r} "
            f"needs at least {p['min_quantity']} selection(s){loc}, got {p['selected_quantity']}"
        )
        if p["available_sub_kits"]:
            lines.append(f"    available sub_kits: {p['available_sub_kits']}")
        if p["available_items"]:
            preview = p["available_items"][:8]
            more = "" if len(p["available_items"]) == len(preview) else f" (+{len(p['available_items'])-len(preview)} more)"
            lines.append(f"    available items: {preview}{more}")
    lines.append("Call get_item_details(kit_id) to see the full decision tree.")
    return "\n".join(lines)


@mcp.tool()
async def add_to_cart(
    kit_id: Annotated[int, Field(description="kit_id from get_item_details")],
    selections: Annotated[
        dict[int, dict[int, Any]],
        Field(
            description=(
                "Modifier selections: {kit_content_id: {entity_id: spec}}.\n"
                "  - entity_id is an item_id from itemList OR a kit_id from a kitList "
                "    (sub-kits like Small/Medium/Large).\n"
                "  - spec is either:\n"
                "      • an int (quantity, attribute defaults to Regular if present)\n"
                "      • a dict like {\"quantity\": 1, \"attribute\": \"Extra\"} for "
                "        toppings that have a Light/Regular/Extra picker.\n"
                "Required for groups with min_quantity > 0. Call get_item_details first."
            ),
        ),
    ] = {},
    quantity: Annotated[int, Field(description="Number of this kit to add", ge=1)] = 1,
    made_for: Annotated[
        str | None,
        Field(description="Optional name to label this item with (\"Who is this for?\"). Only set when the user provides a name."),
    ] = None,
) -> dict[str, Any]:
    """Add an item to the Meals2Go cart with chosen modifier selections.

    Pre-flight validation rejects under-configured items (any required
    modifier group with min_quantity > 0 that has no selection) before
    POSTing, with a structured error listing what's missing and what
    options are available. This prevents the silent-$0-item failure
    mode where Wegmans accepts a half-configured kit.

    For subs/wraps: many topping items have a Light/Regular/Extra picker
    nested under them. Pass {"quantity": 1, "attribute": "Extra"} (or
    "Light"/"Regular") to specify; default is Regular.
    """
    client = _get_client()
    kit = await client.get_kit(kit_id)
    problems = _validate_selections(kit, selections)
    if problems:
        raise ValueError(_format_validation_error(kit_id, problems))
    payload = client.build_add_payload(kit, selections=selections, quantity=quantity)
    cart = await client.add_to_cart(payload, quantity=quantity, made_for=made_for)
    return _summarize_cart(cart)


@mcp.tool()
async def update_cart_item_quantity(
    cart_item_id: Annotated[str, Field(description="cart_item_id from view_cart")],
    quantity: Annotated[int, Field(description="New quantity (use 0 to remove)", ge=0)],
) -> dict[str, Any]:
    """Change the quantity of a cart item. Setting quantity=0 removes it."""
    client = _get_client()
    cart = await client.get_cart()
    target = next((ci for ci in cart.get("cartItems") or [] if ci.get("cartItemId") == cart_item_id), None)
    if target is None:
        raise ValueError(f"No cart item with cart_item_id={cart_item_id!r}")
    updated = await client.patch_cart_item(target, quantity=quantity)
    return _summarize_cart(updated)


_COUPON_SOURCES = ("shop", "meals2go")


def _normalize_shop_coupon(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "offer_id": c.get("id"),
        "title": c.get("description") or c.get("shortDescription"),
        "brand": c.get("brand"),
        "category": c.get("category"),
        "value": c.get("valueText") or c.get("value"),
        "terms": c.get("terms"),
        "min_purchase": c.get("minPurchase"),
        "clipped": (c.get("group") == "clipped") or bool(c.get("clippedDates")),
        "expires": c.get("expirationDate"),
        "clip_ends": c.get("clipEndDate"),
    }


def _normalize_meals2go_coupon(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "offer_id": c.get("id"),
        "title": c.get("copyHeader"),
        "description": c.get("copyText"),
        "terms": c.get("terms"),
        "clipped": bool(c.get("clipped")),
        "expires": c.get("expirationDate"),
        "clip_ends": c.get("clipEndDate"),
        "badge": c.get("badge"),
    }


@mcp.tool()
async def list_coupons(
    source: Annotated[
        str,
        Field(description="Which coupon set: 'shop' (the main wegmans.com Shoppers Club set, 100+ coupons used at grocery checkout — default) or 'meals2go' (the smaller set tied to Meals2Go orders)."),
    ] = "shop",
    only_unclipped: Annotated[bool, Field(description="If true, only return coupons not yet clipped.")] = False,
) -> list[dict[str, Any]]:
    """List Wegmans digital coupons (Shoppers Club offers)."""
    if source not in _COUPON_SOURCES:
        raise ValueError(f"source must be one of {_COUPON_SOURCES}")
    client = _get_client()
    if source == "shop":
        raw = await client.list_shop_coupons()
        out = [_normalize_shop_coupon(c) for c in raw]
    else:
        raw = await client.list_meals2go_coupons(_loyalty_id())
        out = [_normalize_meals2go_coupon(c) for c in raw]
    if only_unclipped:
        out = [c for c in out if not c["clipped"]]
    return out


@mcp.tool()
async def clip_coupons(
    source: Annotated[
        str,
        Field(description="Which coupon set: 'shop' (default) or 'meals2go'."),
    ] = "shop",
    offer_ids: Annotated[
        list[int] | None,
        Field(description="Specific offer_ids to clip. Omit to clip ALL currently unclipped coupons in the chosen source."),
    ] = None,
) -> dict[str, Any]:
    """Clip digital coupons. Pass offer_ids to clip specific ones, or omit to clip all unclipped.

    Defaults to the shop (wegmans.com) coupon set — the big one most users mean
    when they say "clip my coupons". Pass source="meals2go" for the smaller
    Meals2Go-scoped set.
    """
    if source not in _COUPON_SOURCES:
        raise ValueError(f"source must be one of {_COUPON_SOURCES}")
    client = _get_client()
    if source == "shop":
        list_fn = client.list_shop_coupons
        clip_fn = client.clip_shop_coupons
        is_clipped = lambda c: c.get("group") == "clipped" or bool(c.get("clippedDates"))
    else:
        loyalty = _loyalty_id()
        list_fn = lambda: client.list_meals2go_coupons(loyalty)
        clip_fn = lambda ids: client.clip_meals2go_coupons(loyalty, ids)
        is_clipped = lambda c: bool(c.get("clipped"))

    if offer_ids is None:
        offers = await list_fn()
        offer_ids = [int(c["id"]) for c in offers if not is_clipped(c)]
    if not offer_ids:
        return {"source": source, "clipped_now": [], "total_clipped": 0, "message": "Nothing to clip."}
    await clip_fn(offer_ids)
    after = await list_fn()
    return {
        "source": source,
        "clipped_now": offer_ids,
        "total_clipped": sum(1 for c in after if is_clipped(c)),
        "total_unclipped": sum(1 for c in after if not is_clipped(c)),
    }


@mcp.tool()
async def set_cart_item_name(
    cart_item_id: Annotated[str, Field(description="cart_item_id from view_cart")],
    name: Annotated[
        str | None,
        Field(description="Name to label the item with (\"Who is this for?\"). Pass null/empty to clear."),
    ],
) -> dict[str, Any]:
    """Set or clear the per-item name on a cart entry (the UI's "Who is this for?" field)."""
    client = _get_client()
    cart = await client.get_cart()
    target = next((ci for ci in cart.get("cartItems") or [] if ci.get("cartItemId") == cart_item_id), None)
    if target is None:
        raise ValueError(f"No cart item with cart_item_id={cart_item_id!r}")
    modified = dict(target)
    modified["madeFor"] = name or None
    updated = await client.patch_cart_item(modified, quantity=target.get("quantity") or 1)
    return _summarize_cart(updated)


@mcp.tool()
async def remove_from_cart(
    cart_item_id: Annotated[str, Field(description="cart_item_id from view_cart")],
) -> dict[str, Any]:
    """Remove an item from the cart (shortcut for update_cart_item_quantity with quantity=0)."""
    client = _get_client()
    cart = await client.get_cart()
    target = next((ci for ci in cart.get("cartItems") or [] if ci.get("cartItemId") == cart_item_id), None)
    if target is None:
        raise ValueError(f"No cart item with cart_item_id={cart_item_id!r}")
    updated = await client.patch_cart_item(target, quantity=0)
    return _summarize_cart(updated)


@mcp.tool()
async def search_groceries(
    query: Annotated[str, Field(description="Product search text, e.g. 'organic whole milk'")],
    limit: Annotated[int, Field(description="Max results (default 10)")] = 10,
    fulfillment_type: Annotated[
        str | None,
        Field(description="store / pickup / delivery. Defaults to the session's current type."),
    ] = None,
    brand: Annotated[
        str | None, Field(description="Restrict to a brand, e.g. 'Wegmans'")
    ] = None,
    organic_only: Annotated[bool, Field(description="Only items tagged Organic")] = False,
) -> dict[str, Any]:
    """Search the Wegmans grocery catalog (packaged goods, produce, dairy —
    NOT the Meals2Go prepared-food menu, which is `list_menu_categories`).

    Prices and availability are specific to the current store and channel.
    Returns `sku_id` values for use with `get_grocery_product`.
    """
    client = _get_client()
    extra = []
    if brand:
        safe = brand.replace('"', '')
        extra.append(f'consumerBrandName:"{safe}"')
    if organic_only:
        extra.append('filterTags:"Organic"')

    result = await client.search_grocery(
        query,
        limit=limit,
        fulfillment_type=fulfillment_type,
        extra_filters=" AND ".join(extra) if extra else None,
    )
    channel = client._grocery_channel(fulfillment_type)
    return {
        "query": query,
        "store_id": client.store_id,
        "channel": channel,
        "total_matches": result.get("nbHits"),
        "results": [_summarize_product(h, channel) for h in result.get("hits") or []],
    }


@mcp.tool()
async def get_grocery_product(
    sku_id: Annotated[str, Field(description="sku_id from search_groceries")],
    fulfillment_type: Annotated[
        str | None, Field(description="store / pickup / delivery")
    ] = None,
) -> dict[str, Any]:
    """Get full detail for one grocery product: both channel prices, nutrition,
    ingredients, allergens, and aisle location at the current store.
    """
    client = _get_client()
    hit = await client.get_grocery_product(sku_id, fulfillment_type=fulfillment_type)
    if hit is None:
        raise ValueError(
            f"No product with sku_id={sku_id!r} at store {client.store_id}. "
            "It may not be carried at this store — try search_groceries."
        )
    channel = client._grocery_channel(fulfillment_type)
    detail = _summarize_product(hit, channel)
    detail.update({
        "upc": hit.get("upc"),
        "description": hit.get("productDescription"),
        "ingredients": hit.get("ingredients"),
        "allergens": hit.get("allergensAndWarnings"),
        "nutrition": hit.get("nutrition"),
        "instructions": hit.get("instructions"),
        "shelf": (hit.get("planogram") or {}).get("shelf"),
        "ebt_eligible": hit.get("ebtEligible"),
        "min_age_to_buy": hit.get("requiredMinimumAgeToBuy"),
        "prices_by_channel": {
            name: {
                "amount": (hit.get(field) or {}).get("amount"),
                "unit_price": (hit.get(field) or {}).get("unitPrice"),
            }
            for name, field in GROCERY_PRICE_FIELDS.items()
            if hit.get(field)
        },
    })
    return detail


def _summarize_grocery_cart(cart: dict[str, Any]) -> dict[str, Any]:
    custom = WegmansClient._cart_custom(cart)
    items = []
    for li in cart.get("lineItems") or []:
        li_custom = {
            f.get("name"): f.get("value")
            for f in ((li.get("custom") or {}).get("customFieldsRaw") or [])
        }
        prices = li.get("lineItemPrice") or {}
        unit_cents = ((li.get("price") or {}).get("value") or {}).get("centAmount")
        planogram = {}
        try:
            planogram = json.loads(li_custom.get("planogram") or "{}")
        except Exception:
            pass
        items.append({
            "sku_id": li.get("productKey"),
            "name": li.get("name"),
            "quantity": li.get("quantity"),
            "unit_price": (unit_cents or 0) / 100 if unit_cents is not None else None,
            "line_total": (prices.get("totalPrice") or 0) / 100,
            "savings": (prices.get("totalSavings") or 0) / 100,
            "is_available": li_custom.get("isAvailable"),
            "aisle": planogram.get("aisle"),
            "note": li_custom.get("note") or None,
        })
    cart_price = cart.get("cartPrice") or {}
    return {
        "cart_id": cart.get("id"),
        "store_number": custom.get("storeNumber"),
        "fulfillment_type": custom.get("fulfillmentType"),
        "item_count": cart.get("totalLineItemQuantity") or 0,
        "total_price": (cart_price.get("totalPrice") or 0) / 100,
        "total_savings": (cart_price.get("totalSavings") or 0) / 100,
        "items": items,
    }


@mcp.tool()
async def view_grocery_cart() -> dict[str, Any]:
    """Get the wegmans.com grocery cart (a.k.a. "My List" when shopping
    in-store). Separate from the Meals2Go prepared-food cart (`view_cart`).
    """
    client = _get_client()
    cart = await client.get_grocery_cart()
    return _summarize_grocery_cart(cart)


@mcp.tool()
async def add_grocery_to_cart(
    sku_id: Annotated[str, Field(description="sku_id from search_groceries")],
    quantity: Annotated[int, Field(description="How many (default 1)")] = 1,
) -> dict[str, Any]:
    """Add a grocery product to the wegmans.com cart at the current store and
    fulfillment channel. Requires a wegmans.com login (auth-shop.json).
    NOT for Meals2Go prepared food — use `add_to_cart` for that.
    """
    client = _get_client()
    cart = await client.add_grocery_to_cart(sku_id, quantity=quantity)
    return _summarize_grocery_cart(cart)


@mcp.tool()
async def update_grocery_cart_item(
    sku_id: Annotated[str, Field(description="sku_id of an item already in the cart")],
    quantity: Annotated[int, Field(description="New quantity; 0 removes the item")],
) -> dict[str, Any]:
    """Change the quantity of a grocery cart item (0 removes it)."""
    client = _get_client()
    cart = await client.update_grocery_quantity(sku_id, quantity)
    return _summarize_grocery_cart(cart)


@mcp.tool()
async def remove_grocery_from_cart(
    sku_id: Annotated[str, Field(description="sku_id of an item already in the cart")],
) -> dict[str, Any]:
    """Remove an item from the grocery cart entirely."""
    client = _get_client()
    cart = await client.remove_grocery_from_cart(sku_id)
    return _summarize_grocery_cart(cart)


# ---- In-chat login (for Claude Desktop, where there is no terminal) ------

_login_task: asyncio.Task | None = None
_login_status: dict[str, Any] = {"state": "idle", "detail": None}


async def _run_login_background() -> None:
    global _client, _auth
    from .login import run_login

    auth_path, shop_path = _auth_paths()

    def on_status(state: str, detail: str) -> None:
        _login_status.update({"state": state, "detail": detail})

    try:
        result = await run_login(
            auth_file=auth_path,
            shop_auth_file=shop_path,
            env_file=auth_path.parent / ".env",
            on_status=on_status,
        )
        # Pick up the fresh session and the auto-detected loyalty number:
        # drop the cached client (it may hold auth sources marked dead) and
        # re-read the .env the login just wrote. No override — a loyalty
        # number the user explicitly configured still wins (blank
        # placeholders were already stripped at startup).
        prev = _client
        _client = None
        _auth = None
        if prev is not None:
            try:
                await prev.aclose()
            except Exception:
                pass
        load_dotenv(auth_path.parent / ".env")
        if result.get("store_id"):
            # The login read the user's real home store off their account —
            # that beats both the built-in default and whatever this session
            # happened to be pointing at.
            _get_client().set_fulfillment(
                result["store_id"],
                result.get("fulfillment_type") or "store",
            )
        elif prev is not None:
            # Nothing detected: keep the store/fulfillment the user had set
            # this session rather than bouncing them back to defaults.
            _get_client().set_fulfillment(prev.store_id, prev.fulfillment_type)
        _login_status.update({
            "state": "done",
            "detail": None,
            "shop_ok": result.get("shop_ok"),
            "loyalty_id": result.get("loyalty_id"),
            "store_id": result.get("store_id"),
            "store_name": result.get("store_name"),
            "fulfillment_type": result.get("fulfillment_type"),
            "saved": result.get("saved"),
        })
    except Exception as e:
        _login_status.update({"state": "failed", "detail": str(e)})


@mcp.tool()
async def setup_wegmans_login(
    restart: Annotated[
        bool,
        Field(description="Cancel a login already in progress and start over "
                          "(use when the user closed the window or wants to retry)"),
    ] = False,
) -> dict[str, Any]:
    """Open a Wegmans sign-in window on this computer (one-time setup, or
    when the saved session has expired). A real browser window appears;
    the user signs in with their own Wegmans account; the session is then
    saved locally so every other tool works. Check progress with
    `check_login_status`. Takes up to a couple of minutes; the first run
    may also download a browser.
    """
    global _login_task

    if _login_task is not None and not _login_task.done():
        if not restart:
            return {
                "status": "already_running",
                **_login_status,
                "next": "Ask the user to finish signing in, then call "
                        "check_login_status. Pass restart=true to abandon "
                        "the current attempt and open a fresh window.",
            }
        _login_task.cancel()
        try:
            await _login_task
        except (asyncio.CancelledError, Exception):
            pass
    _login_status.clear()
    _login_status.update({"state": "starting", "detail": None})
    _login_task = asyncio.create_task(_run_login_background())
    return {
        "status": "started",
        "instructions": (
            "A browser window is opening on the user's computer. Tell them to "
            "click 'Sign In' (top right of meals2go.com) and log in with their "
            "Wegmans account, then come back here. Poll check_login_status to "
            "see when it finishes."
        ),
    }


@mcp.tool()
async def check_login_status() -> dict[str, Any]:
    """Check the state of a sign-in started by `setup_wegmans_login`, and
    whether saved Wegmans sessions exist on disk at all."""
    auth_path, shop_path = _auth_paths()
    out = dict(_login_status)
    out["auth_file_exists"] = auth_path.exists()
    out["shop_auth_file_exists"] = shop_path.exists()
    if out.get("state") == "done":
        if out.get("shop_ok"):
            out["next"] = (
                "Login complete — all tools are ready. If the user hasn't set "
                "a home store yet, offer to find their store (search_stores) "
                "and set it (set_fulfillment)."
            )
        else:
            out["next"] = (
                "Meals2Go login complete — catalog search and prepared-food "
                "ordering are ready, but the grocery-site sign-in didn't "
                "finish, so grocery cart tools may not work"
                + (" (an older saved grocery session exists and may still "
                   "work)" if out["shop_auth_file_exists"] else "")
                + ". Run setup_wegmans_login again to retry."
            )
    elif out.get("state") == "failed" and not out.get("auth_file_exists"):
        out["next"] = "Run setup_wegmans_login again to retry."
    return out


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
