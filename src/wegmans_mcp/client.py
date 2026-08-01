"""Thin HTTP wrapper around the Wegmans Meals2Go (wegapi.azure-api.net) API.

Authentication and the APIM subscription key are attached on every call.
The store_id is fixed at construction time (Meals2Go is store-scoped).
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any

import httpx

from .auth import FallbackAuth, WegmansAuth

WEGAPI_BASE = "https://wegapi.azure-api.net"
WEGMANS_CLOUD_BASE = "https://api.digitaldevelopment.wegmans.cloud"
WEGMANS_WWW_BASE = "https://www.wegmans.com"
APIM_SUBSCRIPTION_KEY = "5197901a4fb04988a35800505266ef1c"

# The grocery catalog on wegmans.com is served by Algolia, not by a Wegmans
# API. The app id and search-only key are embedded in the public page bundle.
ALGOLIA_APP_ID = "QGPPR19V8V"
ALGOLIA_SEARCH_KEY = "9a10b1401634e9a6e55161c3a60c200d"
ALGOLIA_BASE = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net"
ALGOLIA_PRODUCTS_INDEX = "products"

# Grocery channels. The Algolia field is spelled `fulfilmentType` (single 'l'),
# unlike the Meals2Go `fulfillmentType`.
GROCERY_CHANNELS: dict[str, str] = {
    "store": "instore",
    "carryout": "instore",
    "instore": "instore",
    "pickup": "pickup",
    "curbside": "pickup",
    "delivery": "delivery",
}

# Only two price blocks exist per product. Pickup is priced at the in-store
# rate; delivery carries a markup (~15% on observed items).
GROCERY_PRICE_FIELDS: dict[str, str] = {
    "instore": "price_inStore",
    "pickup": "price_inStore",
    "delivery": "price_delivery",
}
CART_API_VERSION = "2020-10-07"
KITTING_API_VERSION = "2021-02-01"
LOCATION_API_VERSION = "2020-09-09"
SHOP_COUPONS_API_VERSION = "2024-11-05-preview"

DEFAULT_STORE_ID = 91  # Amherst St., Buffalo NY
DEFAULT_STOREFRONT_ID = 1
DEFAULT_ORGANIZATION_ID = 1
DEFAULT_FULFILLMENT_TYPE = "store"
DEFAULT_RADIUS = "standard"

# Maps API surface names (and friendly aliases) to the wire value
FULFILLMENT_TYPES: dict[str, str] = {
    "store": "store",         # in-store pickup
    "carryout": "store",      # alias used in UI
    "instore": "store",       # alias used by wegmans.com
    "curbside": "curbside",
    "pickup": "curbside",     # alias used by wegmans.com
    "delivery": "delivery",
}


def normalize_fulfillment(value: str) -> str:
    key = value.lower().strip()
    if key not in FULFILLMENT_TYPES:
        raise ValueError(
            f"Unknown fulfillment type {value!r}. "
            f"Expected one of: {sorted(set(FULFILLMENT_TYPES))}"
        )
    return FULFILLMENT_TYPES[key]


class WegmansClient:
    def __init__(
        self,
        auth: WegmansAuth,
        store_id: int = DEFAULT_STORE_ID,
        storefront_id: int = DEFAULT_STOREFRONT_ID,
        organization_id: int = DEFAULT_ORGANIZATION_ID,
        fulfillment_type: str = DEFAULT_FULFILLMENT_TYPE,
        radius: str = DEFAULT_RADIUS,
        shop_auth: WegmansAuth | None = None,
    ):
        self.auth = auth
        self.shop_auth = shop_auth
        # Cloud (commerce) calls prefer the shop token but fall back to the
        # Meals2Go token if the shop mint fails — the coupon endpoints accept
        # either, and FallbackAuth remembers a dead source so the ~30s failed
        # mint is paid at most once per session.
        self._cloud_auth = (
            FallbackAuth(shop_auth, auth) if shop_auth is not None else auth
        )
        self.store_id = store_id
        self.storefront_id = storefront_id
        self.organization_id = organization_id
        self.fulfillment_type = fulfillment_type
        self.radius = radius
        self._http = httpx.AsyncClient(base_url=WEGAPI_BASE, timeout=30)
        self._commerce_customer: dict[str, Any] | None = None
        self._store_keys: dict[int, str] | None = None
        self._assistant: Any | None = None

    WEGMANS_CLOUD_BASE = WEGMANS_CLOUD_BASE

    @property
    def assistant(self):
        """Lazily-built conversation with the wegmans.com AI assistant.
        Kept on the client so follow-up turns stay in the same session."""
        if self._assistant is None:
            from .assistant import WegmansAssistant

            self._assistant = WegmansAssistant(self)
        return self._assistant

    def reset_assistant(self) -> None:
        self._assistant = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "WegmansClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = await self.auth.get_token()
        headers = kwargs.pop("headers", {}) or {}
        headers.update({
            "Authorization": f"Bearer {token}",
            "Ocp-Apim-Subscription-Key": APIM_SUBSCRIPTION_KEY,
            "X-Include-Auth": "",
            "Accept": "application/json",
        })
        if "json" in kwargs:
            headers.setdefault("Content-Type", "application/json")
        r = await self._http.request(method, path, headers=headers, **kwargs)
        r.raise_for_status()
        return r

    # ---- Cart ------------------------------------------------------------

    def _cart_path(self, *suffix: str) -> str:
        parts = [
            "cart", "organizations", str(self.organization_id),
            "stores", str(self.store_id), "storefronts", str(self.storefront_id),
            *suffix,
        ]
        return "/" + "/".join(parts)

    def _cart_params(self, include_unavailable: bool = True) -> dict[str, str]:
        return {
            "api-version": CART_API_VERSION,
            "includeUnavailableItems": "true" if include_unavailable else "false",
            "fulfillmentType": self.fulfillment_type,
            "radius": self.radius,
        }

    async def get_cart(self) -> dict[str, Any]:
        r = await self._request("GET", self._cart_path(), params=self._cart_params())
        return r.json()

    async def add_to_cart(self, payload: dict[str, Any], quantity: int = 1, made_for: str | None = None) -> dict[str, Any]:
        body = {
            "includeUnavailableItems": True,
            "fulfillmentType": self.fulfillment_type,
            "radius": self.radius,
            "cartItems": [{
                "cartItemId": payload.get("wooKitId"),  # client-side UUID; server returns its own
                "quantity": quantity,
                "schema": "KIT",
                "madeFor": made_for,
                "payload": payload,
            }],
        }
        r = await self._request(
            "POST",
            self._cart_path("cart-items"),
            params={"api-version": CART_API_VERSION},
            json=body,
        )
        return r.json()

    async def patch_cart_item(self, cart_item: dict[str, Any], quantity: int) -> dict[str, Any]:
        body = {
            "fulfillmentType": self.fulfillment_type,
            "includeUnavailableItems": True,
            "cartItems": [{
                "cartItemId": cart_item["cartItemId"],
                "quantity": quantity,
                "schema": cart_item.get("schema", "KIT"),
                "madeFor": cart_item.get("madeFor"),
                "note": cart_item.get("note"),
                "payload": cart_item["payload"],
            }],
        }
        r = await self._request(
            "PATCH",
            self._cart_path("cart-items"),
            params={"api-version": CART_API_VERSION},
            json=body,
        )
        return r.json()

    # ---- Menu (kitting) --------------------------------------------------

    def _kitting_path(self, *suffix: str) -> str:
        parts = ["kitting", "stores", str(self.store_id), *suffix]
        return "/" + "/".join(parts)

    async def get_menu(self, catering: bool = False) -> dict[str, Any]:
        r = await self._request(
            "GET",
            self._kitting_path("storefronts", str(self.storefront_id), "menus"),
            params={"catering": str(catering).lower(), "radius": self.radius, "api-version": KITTING_API_VERSION},
        )
        return r.json()

    async def get_menu_children(self, menu_id: int, menu_content_id: int) -> dict[str, Any]:
        r = await self._request(
            "GET",
            self._kitting_path(
                "storefronts", str(self.storefront_id),
                "menus", str(menu_id),
                "menu-contents", str(menu_content_id), "children",
            ),
            params={"api-version": KITTING_API_VERSION},
        )
        return r.json()

    async def get_kit(self, kit_id: int) -> dict[str, Any]:
        r = await self._request(
            "GET",
            self._kitting_path("kits", str(kit_id)),
            params={"api-version": KITTING_API_VERSION},
        )
        return r.json()

    # ---- Locations -------------------------------------------------------

    # ---- Digital coupons (Meals2Go side, ~10 coupons, loyalty-scoped) ----

    async def list_meals2go_coupons(self, loyalty_id: str) -> list[dict[str, Any]]:
        r = await self._request(
            "GET",
            f"/digital-coupons/organizations/{self.organization_id}/loyalty/{loyalty_id}",
            params={"api-version": "2020-08-24", "Subscription-Key": APIM_SUBSCRIPTION_KEY},
        )
        return (r.json() or {}).get("coupons") or []

    async def clip_meals2go_coupons(self, loyalty_id: str, offer_ids: list[int]) -> dict[str, Any]:
        r = await self._request(
            "POST",
            f"/digital-coupons/organizations/{self.organization_id}/loyalty/{loyalty_id}",
            params={"api-version": "2020-08-24", "Subscription-Key": APIM_SUBSCRIPTION_KEY},
            json=offer_ids,
        )
        try:
            return r.json() or {}
        except Exception:
            return {}

    # ---- Digital coupons (Shop side, 100+ coupons, JWT-scoped) -----------

    async def _cloud_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Hit the shop backend (api.digitaldevelopment.wegmans.cloud).

        Prefers the shop-site token (which carries the Commerce/Instacart
        scopes); falls back to the Meals2Go token, which the coupon endpoints
        accept because the two apps share an audience. No APIM key needed.
        """
        token = await self._cloud_auth.get_token()
        headers = kwargs.pop("headers", {}) or {}
        headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        if "json" in kwargs:
            headers.setdefault("Content-Type", "application/json")
        url = WEGMANS_CLOUD_BASE + path
        r = await self._http.request(method, url, headers=headers, **kwargs)
        r.raise_for_status()
        return r

    async def list_shop_coupons(self, size: int = 500) -> list[dict[str, Any]]:
        """List Wegmans shop-side digital coupons (the big set used at grocery
        checkout — typically 100+). Each item has a `group` of "available"
        or "clipped".
        """
        r = await self._cloud_request(
            "GET",
            "/commerce/digital-coupons/offers",
            params={"api-version": SHOP_COUPONS_API_VERSION, "size": str(size)},
        )
        return (r.json() or {}).get("items") or []

    async def clip_shop_coupons(self, offer_ids: list[int]) -> dict[str, Any]:
        r = await self._cloud_request(
            "POST",
            "/commerce/digital-coupons/offers/clip",
            params={"api-version": SHOP_COUPONS_API_VERSION},
            json=offer_ids,
        )
        try:
            return r.json() or {}
        except Exception:
            return {}

    # ---- Grocery catalog (wegmans.com / Algolia) -------------------------

    def _grocery_channel(self, fulfillment_type: str | None = None) -> str:
        key = (fulfillment_type or self.fulfillment_type).lower().strip()
        if key not in GROCERY_CHANNELS:
            raise ValueError(
                f"Unknown fulfillment type {fulfillment_type!r}. "
                f"Expected one of: {sorted(set(GROCERY_CHANNELS))}"
            )
        return GROCERY_CHANNELS[key]

    async def search_grocery(
        self,
        query: str,
        limit: int = 10,
        fulfillment_type: str | None = None,
        extra_filters: str | None = None,
        page: int = 0,
    ) -> dict[str, Any]:
        """Search the grocery catalog for the current store.

        Anonymous — the catalog index needs no Bearer token, only the public
        search key. Availability and price are per store and per channel, so
        both are baked into the Algolia filter rather than passed as a query.
        """
        channel = self._grocery_channel(fulfillment_type)
        filters = (
            f"storeNumber:{self.store_id} AND fulfilmentType:{channel} "
            f"AND excludeFromWeb:false AND isSoldAtStore:true"
        )
        if extra_filters:
            filters = f"{filters} AND ({extra_filters})"

        r = await self._http.post(
            f"{ALGOLIA_BASE}/1/indexes/*/queries",
            params={
                "x-algolia-api-key": ALGOLIA_SEARCH_KEY,
                "x-algolia-application-id": ALGOLIA_APP_ID,
            },
            json={"requests": [{
                "indexName": ALGOLIA_PRODUCTS_INDEX,
                "query": query,
                "hitsPerPage": limit,
                "page": page,
                "filters": filters,
            }]},
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        return (r.json().get("results") or [{}])[0]

    async def get_grocery_product(
        self, sku_id: str, fulfillment_type: str | None = None
    ) -> dict[str, Any] | None:
        """Fetch one catalog product by SKU for the current store.

        Index objects are keyed `{storeNumber}-{skuId}`, so this is an exact
        lookup rather than a search.
        """
        channel = self._grocery_channel(fulfillment_type)
        r = await self._http.post(
            f"{ALGOLIA_BASE}/1/indexes/*/queries",
            params={
                "x-algolia-api-key": ALGOLIA_SEARCH_KEY,
                "x-algolia-application-id": ALGOLIA_APP_ID,
            },
            json={"requests": [{
                "indexName": ALGOLIA_PRODUCTS_INDEX,
                "query": "",
                "hitsPerPage": 1,
                "filters": (
                    f'objectID:"{self.store_id}-{sku_id}" '
                    f"AND fulfilmentType:{channel}"
                ),
            }]},
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        hits = (r.json().get("results") or [{}])[0].get("hits") or []
        return hits[0] if hits else None

    # ---- Grocery cart (commerce backend, captured 2026-08-01) ------------
    #
    # The wegmans.com cart is a commercetools cart behind
    # /commerce/cart/carts/. All mutations echo cartID + cartVersion
    # (optimistic concurrency — the server bumps version on every write,
    # so each mutation must re-GET the cart first). The in-store "My List"
    # and the pickup/delivery cart are the same object; which one the UI
    # shows is just the cart's fulfillmentType custom field.

    COMMERCE_CART_API_VERSION = "2024-02-19-preview"

    def _require_shop_auth(self) -> None:
        if self.shop_auth is None:
            raise RuntimeError(
                "Grocery cart operations need a wegmans.com login "
                "(auth-shop.json). Run `uv run python scripts/setup_login.py` "
                "to create it."
            )

    async def _get_commerce_customer(self) -> dict[str, Any]:
        if self._commerce_customer is None:
            r = await self._cloud_request(
                "GET", "/commerce/account/customer",
                params={"api-version": "2024-03-06-preview"},
            )
            customer = (r.json() or {}).get("customer") or {}
            if not customer.get("id"):
                raise RuntimeError("commerce customer lookup returned no id")
            self._commerce_customer = customer
        return self._commerce_customer

    async def _get_store_key(self, store_id: int | None = None) -> str:
        """StoreKey like '91-AMHERST-ST', from the wegmans.com store list."""
        sid = int(store_id if store_id is not None else self.store_id)
        if self._store_keys is None:
            r = await self._http.get(
                f"{WEGMANS_WWW_BASE}/api/stores",
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            )
            r.raise_for_status()
            self._store_keys = {
                int(s["storeNumber"]): s["key"]
                for s in r.json()
                if s.get("storeNumber") is not None and s.get("key")
            }
        if sid not in self._store_keys:
            raise ValueError(f"No wegmans.com store with storeNumber {sid}")
        return self._store_keys[sid]

    async def get_grocery_cart(self) -> dict[str, Any]:
        """Fetch the grocery cart. The server creates an empty cart if none
        exists, so this never 404s for a valid account."""
        self._require_shop_auth()
        r = await self._cloud_request(
            "GET", "/commerce/cart/carts/",
            params={"api-version": self.COMMERCE_CART_API_VERSION},
        )
        body = r.json() or {}
        if body.get("hasErrorMessage"):
            raise RuntimeError(f"cart fetch failed: {body.get('errormessage')}")
        cart = body.get("grocery")
        if not cart or not cart.get("id"):
            raise RuntimeError("cart fetch returned no grocery cart")
        return cart

    @staticmethod
    def _cart_custom(cart: dict[str, Any]) -> dict[str, Any]:
        return {
            f.get("name"): f.get("value")
            for f in ((cart.get("custom") or {}).get("customFieldsRaw") or [])
        }

    async def _ensure_grocery_context(self, cart: dict[str, Any]) -> dict[str, Any]:
        """Align the server-side cart's store/fulfillment with this client's
        state (changestore), returning the refreshed cart. Items survive the
        switch — only pricing/availability context changes, matching the
        wegmans.com UI behavior."""
        wanted_store = str(self.store_id)
        wanted_type = self._grocery_channel()
        current = self._cart_custom(cart)
        if (current.get("storeNumber") == wanted_store
                and current.get("fulfillmentType") == wanted_type):
            return cart

        customer = await self._get_commerce_customer()
        body = {
            "cartData": [{
                "cartID": cart["id"],
                "cartVersion": cart["version"],
                "custom": [
                    {"name": "storeNumber", "value": wanted_store},
                    {"name": "fulfillmentType", "value": wanted_type},
                ],
                "isAlcoholic": bool(cart.get("isAlcoholic")),
                "lineItems": [],
                "customLineItems": [],
            }],
            "customerEmail": customer.get("email"),
            "customerId": customer["id"],
            "storeChanged": current.get("storeNumber") != wanted_store,
            "storeKey": await self._get_store_key(),
        }
        r = await self._cloud_request(
            "PUT", "/commerce/cart/carts/changestore",
            params={"api-version": self.COMMERCE_CART_API_VERSION},
            json=body,
        )
        envelope = r.json() or {}
        if envelope.get("hasErrorMessage") or envelope.get("errors"):
            raise RuntimeError(
                "cart store/fulfillment switch failed: "
                f"{envelope.get('errormessage') or envelope.get('errors')}"
            )
        return envelope.get("grocery") or await self.get_grocery_cart()

    def _build_grocery_line_item(
        self, hit: dict[str, Any], quantity: int
    ) -> dict[str, Any]:
        """Shape an Algolia product hit into the cart line-item envelope the
        SPA sends. Field-for-field from docs/captures/grocery-cart-add-request.json."""
        channel = self._grocery_channel()
        price_block = hit.get(GROCERY_PRICE_FIELDS[channel]) or hit.get("price_inStore") or {}
        amount = price_block.get("amount")
        if amount is None:
            raise ValueError(
                f"Product {hit.get('skuId')} has no {channel} price at store "
                f"{self.store_id} — it may not be sellable in this channel."
            )
        category = (hit.get("category") or [])
        root_category = category[-1] if category else {}
        planogram = hit.get("planogram") or {}
        custom = [
            {"name": "category", "value": root_category.get("name") or ""},
            {"name": "categoryId", "value": root_category.get("key") or ""},
            {"name": "itemLevelAdjustments", "value": "[]"},
            {"name": "isSoldAtStore", "value": bool(hit.get("isSoldAtStore"))},
            {"name": "ebtEligible", "value": bool(hit.get("ebtEligible"))},
            {"name": "isAvailable", "value": bool(hit.get("isAvailable"))},
            {"name": "planogram", "value": json.dumps(
                {
                    "aisle": planogram.get("aisle"),
                    "shelf": planogram.get("shelf"),
                    "aisleSide": planogram.get("aisleSide"),
                    "category": planogram.get("category"),
                    "section": planogram.get("section"),
                    "sortRank": planogram.get("sortRank"),
                },
                separators=(",", ":"),
            )},
            {"name": "note", "value": ""},
            {"name": "bottleDeposit", "value": hit.get("bottleDeposit") or 0},
            {"name": "upc", "value": hit.get("upc") or []},
            {"name": "fulfillmentTypes", "value": hit.get("fulfilmentType") or []},
            {"name": "maxQuantity", "value": str(hit.get("maxQuantity") or 99)},
        ]
        return {
            "custom": custom,
            "distributionChannelKey": price_block.get("channelKey")
            or f"{self.store_id}-Instore",
            "isAlcoholic": bool(hit.get("isAlcoholItem")),
            "isSoldByWeight": bool(hit.get("isSoldByWeight")),
            "onlineApproxUnitWeight": hit.get("onlineApproxUnitWeight") or 0,
            "onlineSellByUnit": hit.get("onlineSellByUnit") or "ea",
            "quantity": quantity,
            "sku": str(hit.get("skuId")),
            "standalonePrice": round(float(amount) * 100),
        }

    async def add_grocery_to_cart(
        self, sku_id: str, quantity: int = 1
    ) -> dict[str, Any]:
        """Add a catalog product to the grocery cart at the current store and
        fulfillment channel. Returns the updated cart."""
        self._require_shop_auth()
        if quantity < 1:
            raise ValueError("quantity must be >= 1 (use remove to delete)")
        hit = await self.get_grocery_product(sku_id)
        if hit is None:
            raise ValueError(
                f"No product with sku_id={sku_id!r} at store {self.store_id}."
            )
        max_qty = int(hit.get("maxQuantity") or 99)
        if quantity > max_qty:
            raise ValueError(
                f"Quantity {quantity} exceeds the per-order limit of {max_qty} "
                f"for {hit.get('productName')!r}."
            )
        cart = await self._ensure_grocery_context(await self.get_grocery_cart())
        customer = await self._get_commerce_customer()
        body = {
            "StoreKey": await self._get_store_key(),
            "cartData": [{
                "cartID": cart["id"],
                "cartVersion": cart["version"],
                "custom": [
                    {"name": "orderLevelAdjustments", "value": "[]"},
                    {"name": "storeNumber", "value": str(self.store_id)},
                    {"name": "fulfillmentType", "value": self._grocery_channel()},
                ],
                "isAlcoholic": bool(cart.get("isAlcoholic")),
                "lineItems": [self._build_grocery_line_item(hit, quantity)],
            }],
            "customerEmail": customer.get("email"),
            "customerID": customer["id"],
        }
        r = await self._cloud_request(
            "POST", "/commerce/cart/carts/lineitems",
            params={"api-version": self.COMMERCE_CART_API_VERSION},
            json=body,
        )
        return self._grocery_mutation_result(r)

    async def update_grocery_quantity(
        self, sku_id: str, quantity: int
    ) -> dict[str, Any]:
        """Set the quantity of a cart line item. quantity=0 removes it."""
        self._require_shop_auth()
        if quantity == 0:
            return await self.remove_grocery_from_cart(sku_id)
        cart = await self._ensure_grocery_context(await self.get_grocery_cart())
        line = self._find_line_item(cart, sku_id)
        li_custom = {
            f.get("name"): f.get("value")
            for f in ((line.get("custom") or {}).get("customFieldsRaw") or [])
        }
        max_qty = int(li_custom.get("maxQuantity") or 99)
        if quantity > max_qty:
            raise ValueError(
                f"Quantity {quantity} exceeds the per-order limit of {max_qty}."
            )
        unit_cents = ((line.get("price") or {}).get("value") or {}).get("centAmount")
        variant_attrs = {
            a.get("name"): a.get("value")
            for a in ((line.get("variant") or {}).get("attributesRaw") or [])
        }
        body = {
            "cartData": [{
                "cartID": cart["id"],
                "cartVersion": cart["version"],
                "isAlcoholic": bool(cart.get("isAlcoholic")),
                "lineItems": [{
                    "centAmount": unit_cents,
                    "custom": [
                        {"name": "isSoldAtStore", "value": bool(li_custom.get("isSoldAtStore", True))},
                        {"name": "isAvailable", "value": bool(li_custom.get("isAvailable", True))},
                        {"name": "itemLevelAdjustments", "value": "[]"},
                    ],
                    "isSoldByWeight": bool(variant_attrs.get("isSoldByWeight")),
                    "maxQtyAllowed": max_qty,
                    "onlineApproxUnitWeight": variant_attrs.get("onlineApproxUnitWeight") or 0,
                    "onlineSellByUnit": variant_attrs.get("onlineSellByUnit") or "ea",
                    "quantity": quantity,
                    "sku": str(sku_id),
                    "standalonePrice": unit_cents,
                }],
            }],
        }
        r = await self._cloud_request(
            "PUT", "/commerce/cart/carts/lineitems/quantity",
            params={"api-version": self.COMMERCE_CART_API_VERSION},
            json=body,
        )
        return self._grocery_mutation_result(r)

    async def remove_grocery_from_cart(self, sku_id: str) -> dict[str, Any]:
        self._require_shop_auth()
        cart = await self._ensure_grocery_context(await self.get_grocery_cart())
        self._find_line_item(cart, sku_id)  # raises if absent
        body = {
            "cartData": [{
                "cartID": cart["id"],
                "cartVersion": cart["version"],
                "custom": [{"name": "orderLevelAdjustments", "value": "[]"}],
                "isAlcoholic": bool(cart.get("isAlcoholic")),
                "lineItems": [{"sku": str(sku_id)}],
            }],
        }
        r = await self._cloud_request(
            "PUT", "/commerce/cart/carts/itemdeletion",
            params={"api-version": self.COMMERCE_CART_API_VERSION},
            json=body,
        )
        return self._grocery_mutation_result(r)

    @staticmethod
    def _find_line_item(cart: dict[str, Any], sku_id: str) -> dict[str, Any]:
        for li in cart.get("lineItems") or []:
            if str(li.get("productKey")) == str(sku_id):
                return li
        skus = [li.get("productKey") for li in cart.get("lineItems") or []]
        raise ValueError(
            f"No cart item with sku {sku_id!r}. Cart contains: {skus or 'nothing'}"
        )

    def _grocery_mutation_result(self, r: httpx.Response) -> dict[str, Any]:
        body = r.json() or {}
        if body.get("hasErrorMessage") or body.get("errors"):
            raise RuntimeError(
                f"cart mutation failed: {body.get('errormessage') or body.get('errors')}"
            )
        cart = body.get("grocery")
        if not cart:
            raise RuntimeError("cart mutation returned no cart body")
        return cart

    # ---- Personalized shopping surfaces (the wegmans.com home page) -------

    async def list_my_items(self, limit: int = 25) -> list[dict[str, Any]]:
        """The customer's "My Items" — what they actually buy, ranked by the
        same signal the site's home page uses. Returns item numbers plus
        purchase recency; call `get_products_by_sku` to price them.
        """
        self._require_shop_auth()
        r = await self._cloud_request(
            "GET", "/commerce/my-items", params={"api-version": "2024-01-26"},
        )
        items = r.json() or []
        items.sort(key=lambda i: i.get("rank") or 10**9)
        return items[:limit]

    async def get_products_by_sku(self, sku_ids: list[str]) -> list[dict[str, Any]]:
        """Batch product lookup on the commerce backend. Same product shape
        as the Algolia catalog, but keyed by SKU — the right call when you
        already know what you want (My Items, a saved list, a past order).
        """
        if not sku_ids:
            return []
        r = await self._cloud_request(
            "GET", "/commerce/browse/products/",
            params={
                "productid": ",".join(str(s) for s in sku_ids),
                "storeNumber": str(self.store_id),
                "api-version": "2023-09-22",
            },
        )
        return r.json() or []

    async def list_saved_lists(self) -> list[dict[str, Any]]:
        self._require_shop_auth()
        r = await self._cloud_request(
            "GET", "/commerce/saved-list/savedlists",
            params={"api-version": "2024-02-20-preview"},
        )
        return r.json() or []

    async def list_orders(self, active_only: bool = True) -> dict[str, Any]:
        self._require_shop_auth()
        path = ("/commerce/order/orders/activeorders" if active_only
                else "/commerce/order/orders")
        r = await self._cloud_request(
            "GET", path, params={"api-version": "2024-03-04-preview"},
        )
        return r.json() or {}

    # ---- Locations -------------------------------------------------------

    async def list_locations(self) -> list[dict[str, Any]]:
        # Note: this endpoint uses only the APIM key (no Bearer token).
        r = await self._http.get(
            "/location/locations",
            params={
                "api-version": LOCATION_API_VERSION,
                "Subscription-Key": APIM_SUBSCRIPTION_KEY,
            },
            headers={"Accept": "application/json", "Ocp-Apim-Subscription-Key": APIM_SUBSCRIPTION_KEY},
        )
        r.raise_for_status()
        return r.json().get("locations") or []

    async def geocode(self, query: str) -> tuple[float, float] | None:
        """Resolve a free-text address/zip/city to (lat, lng) via the
        Wegmans Google Geocoding proxy. Returns None if nothing matched.
        """
        r = await self._request(
            "GET",
            "/google/maps/geocode",
            params={"address": query, "api-version": "2019-11-01"},
        )
        results = (r.json() or {}).get("results") or []
        if not results:
            return None
        loc = results[0]["geometry"]["location"]
        return float(loc["lat"]), float(loc["lng"])

    async def search_stores(
        self,
        near: str | None = None,
        fulfillment_type: str | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        stores = await self.list_locations()
        wire_type = normalize_fulfillment(fulfillment_type) if fulfillment_type else None
        if wire_type:
            stores = [s for s in stores if wire_type in (s.get("fulfillmentTypes") or [])]

        origin: tuple[float, float] | None = None
        if near:
            origin = await self.geocode(near)
            if origin is None:
                raise ValueError(f"No place matched query {near!r}")

        results = []
        for s in stores:
            entry = {
                "store_id": int(s.get("locationId")),
                "name": s.get("facilityName"),
                "street": s.get("streetAddressLine1"),
                "city": s.get("city"),
                "state": s.get("state"),
                "postal_code": s.get("postalCode"),
                "latitude": s.get("latitude"),
                "longitude": s.get("longitude"),
                "fulfillment_types": s.get("fulfillmentTypes") or [],
                "phone": s.get("fulfillmentPhoneNumberCopyText"),
            }
            if origin and entry["latitude"] is not None and entry["longitude"] is not None:
                entry["distance_miles"] = _haversine_miles(
                    origin[0], origin[1], entry["latitude"], entry["longitude"]
                )
            results.append(entry)

        if origin:
            results.sort(key=lambda r: r.get("distance_miles") or math.inf)

        return results[:max_results]

    def set_fulfillment(self, store_id: int, fulfillment_type: str) -> dict[str, Any]:
        """Update the cart fulfillment context for subsequent calls.

        Fulfillment is purely client-side state — Wegmans has no SET endpoint.
        We just change what we send as the URL path / query params.
        """
        self.store_id = int(store_id)
        self.fulfillment_type = normalize_fulfillment(fulfillment_type)
        return {"store_id": self.store_id, "fulfillment_type": self.fulfillment_type}

    # ---- Payload construction helpers ------------------------------------

    @staticmethod
    def build_add_payload(
        kit: dict[str, Any],
        selections: dict[int, dict[int, int]] | None = None,
        quantity: int = 1,
    ) -> dict[str, Any]:
        """Take a raw kit (from get_kit) and produce a payload ready for add_to_cart.

        `selections` maps kitContentId → {entity_id: quantity}. entity_id may
        refer to an item in `itemList` OR a sub-kit in `kitList` (some kits,
        like subs, use a Size group whose options are themselves kits).

        Truncation: each `itemList`/`kitList` is pruned to only the chosen
        entities. Leaving unselected entries in causes the server to discard
        all selections silently and mark the cart item unavailable.

        Selections for a sub-kit's own modifier groups go in the same flat
        dict (keyed by their kitContentId) — this function recurses into
        every selected sub-kit's uiNavigationSections.
        """
        payload = copy.deepcopy(kit)
        payload["selectedQuantity"] = quantity
        payload["isSelected"] = True
        _apply_selections(payload.get("uiNavigationSections"), selections or {})
        return payload


def _apply_selections(sections: list | None, selections: dict) -> None:
    for section in sections or []:
        for kc in section.get("kitContents") or []:
            chosen = selections.get(kc.get("kitContentId"), {})
            pruned_items = []
            for item in kc.get("itemList") or []:
                if item.get("itemId") in chosen:
                    spec = chosen[item["itemId"]]
                    qty, attr_id = _unpack_spec(spec)
                    item["isSelected"] = True
                    item["selectedQuantity"] = qty
                    _apply_attribute_selection(item, attr_id)
                    pruned_items.append(item)
            kc["itemList"] = pruned_items

            pruned_kits = []
            for sub_kit in kc.get("kitList") or []:
                if sub_kit.get("kitId") in chosen:
                    spec = chosen[sub_kit["kitId"]]
                    qty, _ = _unpack_spec(spec)
                    sub_kit["isSelected"] = True
                    sub_kit["selectedQuantity"] = qty
                    _apply_selections(sub_kit.get("uiNavigationSections"), selections)
                    pruned_kits.append(sub_kit)
            kc["kitList"] = pruned_kits


def _unpack_spec(spec) -> tuple[int, int | str | None]:
    """A selection value can be an int (quantity, default attribute) or a
    dict like {"quantity": 1, "attribute": "Extra"} / {"quantity": 1, "attribute_id": 137}.
    """
    if isinstance(spec, int):
        return spec, None
    if isinstance(spec, dict):
        return int(spec.get("quantity", 1)), spec.get("attribute_id") or spec.get("attribute")
    raise TypeError(f"Unsupported selection value: {spec!r}")


def _apply_attribute_selection(item: dict, attr_choice: int | str | None) -> None:
    """If the item has itemAttributeSets (e.g., Light/Regular/Extra), truncate
    each set's `attributes` to just the chosen one and flag it selected.
    """
    sets = item.get("itemAttributeSets") or []
    for s in sets:
        attrs = s.get("attributes") or []
        if not attrs:
            continue
        chosen_attr = _pick_attribute(attrs, attr_choice)
        chosen_attr["isSelected"] = True
        chosen_attr["selectedQuantity"] = 1
        s["attributes"] = [chosen_attr]


def _pick_attribute(attrs: list, choice: int | str | None) -> dict:
    if isinstance(choice, int):
        for a in attrs:
            if a.get("attributeId") == choice:
                return a
    if isinstance(choice, str):
        c = choice.strip().lower()
        for a in attrs:
            if (a.get("code") or "").lower() == c:
                return a
    # fall back to the attribute marked as default
    for a in attrs:
        if a.get("defaultAttribute"):
            return a
    return attrs[0]


def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r_miles = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_miles * math.asin(math.sqrt(a))
