"""Thin HTTP wrapper around the Wegmans Meals2Go (wegapi.azure-api.net) API.

Authentication and the APIM subscription key are attached on every call.
The store_id is fixed at construction time (Meals2Go is store-scoped).
"""

from __future__ import annotations

import copy
import math
from typing import Any

import httpx

from .auth import WegmansAuth

WEGAPI_BASE = "https://wegapi.azure-api.net"
WEGMANS_CLOUD_BASE = "https://api.digitaldevelopment.wegmans.cloud"
APIM_SUBSCRIPTION_KEY = "5197901a4fb04988a35800505266ef1c"
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
    "curbside": "curbside",
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
    ):
        self.auth = auth
        self.store_id = store_id
        self.storefront_id = storefront_id
        self.organization_id = organization_id
        self.fulfillment_type = fulfillment_type
        self.radius = radius
        self._http = httpx.AsyncClient(base_url=WEGAPI_BASE, timeout=30)

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
        Uses the same Bearer JWT as the Meals2Go side (audience matches).
        No APIM subscription key needed.
        """
        token = await self.auth.get_token()
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
