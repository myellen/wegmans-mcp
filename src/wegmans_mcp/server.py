"""MCP server exposing Wegmans Meals2Go cart operations as tools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .auth import WegmansAuth
from .client import WegmansClient

mcp = FastMCP("wegmans-mcp")

_auth: WegmansAuth | None = None
_client: WegmansClient | None = None


def _get_client() -> WegmansClient:
    global _auth, _client
    if _client is None:
        auth_path = Path(os.environ.get("WEGMANS_AUTH_FILE", "auth.json"))
        store_id = int(os.environ.get("WEGMANS_STORE_ID", "16"))
        _auth = WegmansAuth(auth_file=auth_path)
        _client = WegmansClient(_auth, store_id=store_id)
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


@mcp.tool()
async def browse_category(
    menu_id: Annotated[int, Field(description="Menu ID (typically 1)")],
    menu_content_id: Annotated[int, Field(description="menuContentId of the category")],
) -> dict[str, Any]:
    """Browse one level deeper into a menu category. Returns the children nodes."""
    client = _get_client()
    node = await client.get_menu_children(menu_id, menu_content_id)
    items: list[dict[str, Any]] = []
    sub_cats: list[dict[str, Any]] = []
    for c in node.get("menuContents") or []:
        rec = {
            "name": c.get("copyHeader"),
            "menu_content_id": c.get("menuContentId"),
            "content_id": c.get("contentId"),
            "is_available": c.get("isFulfillmentAvailable"),
            "price_range": c.get("menuPriceRange"),
            "is_promo": c.get("isPromo"),
        }
        if c.get("contentType") == "Category":
            sub_cats.append(rec)
        else:
            items.append(rec)
    return {
        "menu_content_id": node.get("menuContentId"),
        "sub_categories": sub_cats,
        "items": items,
    }


@mcp.tool()
async def get_item_details(
    kit_id: Annotated[int, Field(description="kit_id of the orderable item")],
) -> dict[str, Any]:
    """Get full details of a menu item, including required modifier groups and prices.

    Use this before add_to_cart so you know which kit_content_ids are required
    and what item_ids you can choose for each modifier group.
    """
    client = _get_client()
    kit = await client.get_kit(kit_id)
    groups = []
    for section in kit.get("uiNavigationSections") or []:
        for kc in section.get("kitContents") or []:
            groups.append({
                "kit_content_id": kc.get("kitContentId"),
                "section_code": section.get("sectionCode"),
                "prompt": kc.get("kitContentCopyHeader"),
                "min_quantity": kc.get("minimumOrderQuantity"),
                "max_quantity": kc.get("maximumOrderQuantity"),
                "allow_multiples": kc.get("allowMultiples"),
                "quantity_at_no_charge": kc.get("quantityAtNoCharge"),
                "options": [
                    {
                        "item_id": it.get("itemId"),
                        "name": it.get("copyHeader"),
                        "price": it.get("price"),
                        "default_choice": it.get("defaultChoice"),
                        "is_available": it.get("isFulfillmentAvailable"),
                    }
                    for it in (kc.get("itemList") or [])
                ],
            })
    return {
        "kit_id": kit.get("kitId"),
        "name": kit.get("copyHeader"),
        "description": kit.get("copyText"),
        "price": kit.get("price"),
        "pricing_method": kit.get("pricingMethod"),
        "product_type": kit.get("productType"),
        "is_available": kit.get("isFulfillmentAvailable"),
        "is_promo": kit.get("isPromo"),
        "modifier_groups": groups,
    }


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

    For subs/wraps: many topping items have a Light/Regular/Extra picker
    nested under them. Pass {"quantity": 1, "attribute": "Extra"} (or
    "Light"/"Regular") to specify; default is Regular.
    """
    client = _get_client()
    kit = await client.get_kit(kit_id)
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


@mcp.tool()
async def list_coupons(
    only_unclipped: Annotated[bool, Field(description="If true, only return coupons not yet clipped.")] = False,
) -> list[dict[str, Any]]:
    """List digital coupons (Shoppers Club offers) for the configured loyalty number."""
    client = _get_client()
    raw = await client.list_coupons(_loyalty_id())
    out = []
    for c in raw:
        if only_unclipped and c.get("clipped"):
            continue
        out.append({
            "offer_id": c.get("id"),
            "title": c.get("copyHeader"),
            "description": c.get("copyText"),
            "terms": c.get("terms"),
            "clipped": bool(c.get("clipped")),
            "expires": c.get("expirationDate"),
            "clip_ends": c.get("clipEndDate"),
            "badge": c.get("badge"),
        })
    return out


@mcp.tool()
async def clip_coupons(
    offer_ids: Annotated[
        list[int] | None,
        Field(description="Specific offer_ids to clip. Omit to clip ALL currently unclipped coupons."),
    ] = None,
) -> dict[str, Any]:
    """Clip digital coupons. Pass offer_ids to clip specific ones, or omit to clip all unclipped."""
    client = _get_client()
    loyalty = _loyalty_id()
    if offer_ids is None:
        all_c = await client.list_coupons(loyalty)
        offer_ids = [int(c["id"]) for c in all_c if not c.get("clipped")]
    if not offer_ids:
        return {"clipped_now": [], "total_clipped": 0, "message": "Nothing to clip."}
    await client.clip_coupons(loyalty, offer_ids)
    after = await client.list_coupons(loyalty)
    return {
        "clipped_now": offer_ids,
        "total_clipped": sum(1 for c in after if c.get("clipped")),
        "total_unclipped": sum(1 for c in after if not c.get("clipped")),
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
