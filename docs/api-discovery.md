# Wegmans Meals2Go API discovery

Captured live from a logged-in browser session on 2026-05-16. Raw HTTP captures in `docs/captures/`.

## Two distinct backends

| Host | Scope | Auth |
|---|---|---|
| `wegapi.azure-api.net` | **Meals2Go** (prepared meals, our target) | Azure B2C JWT + APIM Subscription-Key |
| `api.digitaldevelopment.wegmans.cloud` | shop.wegmans.com (groceries) | Azure B2C JWT (different client_id) |

The Meals2Go cart is on **`wegapi.azure-api.net`**. Everything below is about that backend.

## Authentication

- **Provider**: Microsoft Azure AD B2C
- **Tenant**: `wegmansonline.onmicrosoft.com` (tid `14892770-9ffd-4a38-807e-36292b99339e`)
- **Policy**: `b2c_1a_wegmanssignupsigninwithphoneverification`
- **Issuer**: `https://myaccount.wegmans.com/14892770-9ffd-4a38-807e-36292b99339e/v2.0/`
- **Audience (aud)**: `3f54b60f-22ef-4d8f-9424-4dd945675fdd`
- **Flow**: Authorization Code + PKCE (S256), MSAL.js
- **Token TTL**: 1 hour

Two distinct client_ids (azp) seen:

| Site | Client ID (azp) | Scopes |
|---|---|---|
| meals2go.com | `d35cf2c4-8982-445f-9274-6c9d6ccb22b5` | `Users.Profile.Read` |
| shop.wegmans.com | `38c78f8d-d124-4796-8430-1cd476d9a982` | `Commerce.SignalR`, `InstacartConnect.*`, `Users.Profile.*`, `DigitalCoupons.Offers`, `Google.AddressValidation`, `Feedback.Write` |

Required headers on every Meals2Go API call:

```
Authorization: Bearer <b2c-jwt>
Ocp-Apim-Subscription-Key: 5197901a4fb04988a35800505266ef1c   # public APIM key
X-Include-Auth:                                              # empty value, signals "forward auth"
Accept: application/json, text/plain, */*
```

User JWT claims (verified):
- `sub` — MSAL user UUID
- `extension_Customer_ID` — customer UUID (also the cart's `customerId`)
- `email`, `given_name`, `family_name`
- `tid` — tenant ID

## Cart API

Base path: `/cart/organizations/1/stores/{storeId}/storefronts/1`

| Verb | Path | Body | Effect |
|---|---|---|---|
| GET | `/cart/organizations/1/stores/16/storefronts/1?api-version=2020-10-07&includeUnavailableItems=true&fulfillmentType=store&radius=standard` | — | Read cart |
| POST | `/cart/organizations/1/stores/16/storefronts/1/cart-items?api-version=2020-10-07` | `{includeUnavailableItems, fulfillmentType, radius, cartItems:[...]}` | Add item |
| PATCH | `/cart/organizations/1/stores/16/storefronts/1/cart-items?api-version=2020-10-07` | same shape; set `quantity: 0` to remove or `n` to update | Update / remove |

### Cart-item envelope

```json
{
  "cartItemId": "<UUID, kit.wooKitId on add; server returns its own UUID after add>",
  "quantity": 1,                  // 0 to remove
  "schema": "KIT",
  "madeFor": null,                // optional "who is this for" string
  "note": null,
  "payload": { /* full kit object */ }
}
```

### `payload` shape (verbose "echo-the-kit" pattern)

The server expects the **entire kit definition** echoed back, with chosen options flagged. Workflow:

1. `GET /kitting/stores/{storeId}/kits/{kitId}` → get full kit definition
2. For each chosen modifier in `payload.uiNavigationSections[].kitContents[].itemList[]`:
   - set `isSelected: true`
   - set `selectedQuantity: <n>`
3. On the kit itself: set `selectedQuantity: <qty>`, `isSelected: true`
4. POST or PATCH the whole object as `payload`

Constraints per modifier group (`kitContent`):
- `minimumOrderQuantity` (often 1+ for required groups)
- `maximumOrderQuantity`
- `allowMultiples` (true → can pick same option N times)
- `quantityAtNoCharge` (free count; over this incurs `price` per unit)

### Nesting: kits-inside-kits and item attributes

Some kits (subs, wraps, anything `BASEPLUS`-priced) nest **sub-kits** inside a
modifier group's `kitList`. The classic case is Size on subs:

```
kit 458 "Chicken Tender" (parent)
  uiNavigationSections[0].kitContents[0]  kitContentId 884 "Choose size"
    kitList:
      - kit 411 "Small"   $6.99   (has its own uiNavigationSections — bread/style/...)
      - kit 459 "Medium"  $9.99
      - kit 408 "Large"  $15.99
```

The same `selections` map drives both layers — `{884: {408: 1}}` picks Large,
and the chosen sub-kit's own modifier groups (`672 Choose bread`, etc.) live in
the SAME flat dict at the same level. `build_add_payload()` walks the tree
recursively, truncating each `kitList` to just the chosen sub-kit and recursing
into its sections.

Topping items often carry an `itemAttributeSets` for **Light / Regular / Extra**
(plus "On Side" for mayos):

```
item 1040 Tomatoes
  itemAttributeSets[0]
    attributes:
      - code: "Light"
      - code: "Regular" (defaultAttribute)
      - code: "Extra"
```

To pick a non-default amount, the SPA truncates `itemAttributeSets[0].attributes`
to the single chosen attribute and sets `isSelected: true, selectedQuantity: 1`
on it. Leaving all three in causes the server to silently drop the selection.

**Failure mode:** Wegmans does not validate selections server-side. If you send
a kit-add with no size picked, the server returns 200 with the item priced at
$0 and `isAvailable: false`. The MCP `add_to_cart` tool pre-flight validates
required groups and raises before that ever leaves the wire.

### Response (cart state)

```
cartId, customerId, shoppingTripId, storeFrontId, cartType ("marketCafe"),
status: { canCheckout, cartStatusCopyText, checkoutRequirementsCopyText, itemAvailabilityCopyText },
mode ("individual"|"group"),
cartItems: [{ cartItemId, quantity, payload, globalMergeId, unitPrice, isAvailable, note, madeFor, ... }],
foodTotal, subTotal, totalTaxes, totalPrice, additionalFees, digitalPromotions,
recommendationLists, utensils, cutleryDeepLink
```

## Menu API

All under `/kitting/stores/{storeId}/storefronts/1/`:

| Path | Returns |
|---|---|
| `/menus?catering=false&radius=standard&api-version=2021-02-01` | Top-level menu sections (categories) |
| `/menus/1/menu-contents/{menuContentId}/children?api-version=2021-02-01` | Children of a category — recursive hypermedia |
| `/kits/{kitId}?api-version=2021-02-01` | Full kit definition (item with modifier groups) |
| `/items/{itemId}` | Individual item (used for modifier options) |

Menu nodes are typed by `contentType`: `Category` (has children link) or directly link to a kit. Each node carries `links: [{href, rel}]` HAL-style.

## Fulfillment + store selection

There is **no SET endpoint**. The fulfillment type and store id are purely
client-side state — the SPA changes them as URL/query params on the next
cart call:

- Carryout: `/cart/.../stores/{storeId}/storefronts/1?fulfillmentType=store&...`
- Curbside: `...fulfillmentType=curbside...`
- Delivery: `...fulfillmentType=delivery...`

Stores: `GET /location/locations?api-version=2020-09-09&Subscription-Key={apim}`
(no Bearer token, just the public APIM key). Returns 113 locations with
fields `locationId, facilityName, streetAddressLine1, city, state, postalCode,
latitude, longitude, fulfillmentTypes (subset of ["store","curbside","delivery"]),
fulfillmentPhoneNumber, storefronts`.

Geocoding a city/zip string for distance sorting uses the Wegmans proxy
of Google Places + Geocoding (Bearer token required):

- `GET /google/maps/places?input={q}&components=country:us&api-version=2019-11-01`
- `GET /google/maps/geocode-placeid?place_id={id}&api-version=2019-11-01`

## Other Meals2Go endpoints

- `POST /guest-idp/token?api-version=2020-01-29` — guest (anonymous) token
- `GET /order-capture/organizations/1/order-history?api-version=2025-06-04` — past orders
- `GET /order-capture/delivery-minimum?api-version=2025-06-04`
- `GET /digital-coupons/organizations/1/loyalty/{loyaltyId}?api-version=2020-08-24` — Meals2Go-side coupons (~10, requires loyalty in URL)
- `GET /app-config/client/kv?key=...&api-version=2019-04-24`

## Shop-side coupons (api.digitaldevelopment.wegmans.cloud)

The main wegmans.com Digital Coupons page (used at grocery checkout) lives
on the shop backend, not wegapi. Same Bearer JWT works — the audience
(`3f54b60f-22ef-4d8f-9424-4dd945675fdd`) is shared across both apps even
though their `azp` (client_id) differs. **No APIM Subscription-Key
required** for this backend, and no `X-Include-Auth` header.

- `GET /commerce/digital-coupons/offers?api-version=2024-11-05-preview&size=500` —
  list (paginated; `size=500` returns all in one call for typical users).
  Returns `{items: [{id, description, brand, category, group: "available"|"clipped",
  value, valueText, terms, expirationDate, clipEndDate, ...}], itemCount, pages}`.
- `POST /commerce/digital-coupons/offers/clip?api-version=2024-11-05-preview` —
  body is a JSON array of offer IDs, e.g. `[8280450]`. Bulk-supported.
  Returns 200 (body may be empty).

## Grocery catalog (wegmans.com)

Captured 2026-08-01. `shop.wegmans.com` now 302s to `www.wegmans.com` — the
grocery storefront is a Next.js app on the main domain.

**Product search is not a Wegmans API at all.** It's Algolia, queried
directly from the browser with a search-only key embedded in the public page
bundle. No Bearer token, no APIM key, no cookies — a plain server-side POST
works, which is why `search_groceries` needs no login.

| Field | Value |
|---|---|
| Algolia app id | `QGPPR19V8V` |
| Search-only key | `9a10b1401634e9a6e55161c3a60c200d` |
| Endpoint | `POST https://qgppr19v8v-dsn.algolia.net/1/indexes/*/queries` |
| Index | `products` |

Request body is `{"requests": [{indexName, query, hitsPerPage, page, filters}]}`.

### Store and channel live in the filter string, not the query

Every product is indexed **once per store per channel**, keyed
`objectID = "{storeNumber}-{skuId}"`. Availability and price are therefore
properties of the filter, not of a lookup parameter:

```
storeNumber:91 AND fulfilmentType:instore
  AND excludeFromWeb:false AND isSoldAtStore:true
```

- **`fulfilmentType` is spelled with one `l`** here, unlike Meals2Go's
  `fulfillmentType`. Misspelling it silently returns unfiltered results
  rather than erroring.
- Omitting `excludeFromWeb:false AND isSoldAtStore:true` surfaces products
  the store doesn't actually carry.
- Channel values are `instore` / `pickup` / `delivery`.

**Store IDs are shared between the two backends.** Cross-checking the
Meals2Go `locationId` list against `/api/stores` gives 113 common IDs with
108 exact name matches; the 5 that differ are formatting only (`Amherst St.`
vs `Amherst St`). So `store_id` set via `set_fulfillment` applies to both
prepared food and groceries.

### Pricing

Only two price blocks exist — `price_inStore` and `price_delivery`. Pickup
is billed at the in-store rate (there is no `price_pickup`). Delivery carries
a markup: observed items ran ~15% over in-store ($3.99 → $4.59). Prices also
vary by store: Organic Valley whole milk was $5.69 at store 91, $5.99 at 16.

`price_inStoreLoyalty` / `price_deliveryLoyalty` exist but were empty on
every item sampled; `digitalCouponsOfferIds` is the populated discount path
and feeds directly into the existing `clip_coupons` tool.

### Useful hit fields

`skuId`, `productName`, `consumerBrandName`, `packSize`, `upc`,
`isSoldByWeight`, `onlineSellByUnit`, `maxQuantity`, `filterTags`
(Organic / Gluten Free / Kosher / Wegmans Brand), `wellnessKeys`,
`planogram.aisle`, `categoryNodes.lvl0..lvl2`, `nutrition`, `ingredients`,
`allergensAndWarnings`, `ebtEligible`, `requiredMinimumAgeToBuy`, `slug`
(product URL is `/shop/product/{slug}`).

`filterTags` is what makes dietary substitution work when converting a list
from another chain.

### Other anonymous endpoints (Next.js BFF on www.wegmans.com)

- `GET /api/stores` — all 114 stores, richer than the Meals2Go location
  list (`hasPickup`, `hasDelivery`, `hasPharmacy`, `sellsAlcohol`,
  `aislePositionMapping`, store hours, pickup instructions).
- `GET /api/stores/store-number/{n}` — one store.
- `GET /api/categories/v3/instore/{storeNumber}?categoryKeys=[...]` —
  category tree.

### Grocery cart (captured live 2026-08-01)

A commercetools cart behind `/commerce/cart/carts/` on the commerce
backend. Raw request/response captures in `docs/captures/grocery-cart-*`.
Earlier speculation was wrong on both counts: the cart is plain REST
(SignalR is only used for order-status push), and Instacart Connect only
provides pickup/delivery service options (windows, fees) — not the cart.

**The in-store "My List" and the pickup/delivery cart are the same
object.** Which UI you get is just the cart's `fulfillmentType` custom
field (`instore` / `pickup` / `delivery`). Items survive context switches;
only pricing/availability recompute.

| Operation | Call |
|---|---|
| Read (creates if absent) | `GET /commerce/cart/carts/?api-version=2024-02-19-preview` |
| Add line item | `POST /commerce/cart/carts/lineitems?api-version=...` |
| Change quantity | `PUT /commerce/cart/carts/lineitems/quantity?api-version=...` |
| Remove line item | `PUT /commerce/cart/carts/itemdeletion?api-version=...` |
| Switch store / fulfillment | `PUT /commerce/cart/carts/changestore?api-version=...` |

Mechanics that matter:

- **Optimistic concurrency.** Every mutation echoes `cartID` +
  `cartVersion` from a fresh GET. The server bumps the version several
  times per operation (observed 15 → 33 on a single add), so never reuse
  a stale version.
- **The client sends the product data.** The add payload carries category,
  planogram, UPCs, `standalonePrice` (unit cents), etc. — all sourced from
  the Algolia hit. The server re-prices authoritatively, though: a Hot
  Zone banana sent at 19¢ came back priced 10¢. Client-sent prices cannot
  corrupt totals.
- **`distributionChannelKey`** is the Algolia price block's `channelKey`
  (`91-Instore` / `91-Delivery`).
- **Deletion needs only `{sku}`** in `lineItems` (plus cart id/version).
- **Emptied carts get recycled.** After the last item is removed the next
  GET may return a brand-new cart id at version 1. Don't persist cart ids.
- **Casing trap:** the add body says `customerID`; changestore says
  `customerId`. Sent verbatim per capture.
- `customerEmail`/`customerID` come from
  `GET /commerce/account/customer?api-version=2024-03-06-preview`
  (`customer.id` is the commercetools id; `customer.key` equals the JWT's
  `extension_Customer_ID`).
- `StoreKey` (e.g. `91-AMHERST-ST`) comes from the `key` field in
  `GET https://www.wegmans.com/api/stores`.

### Shop-side auth (works headlessly)

Same silent-renewal strategy as Meals2Go but seeded from a wegmans.com
session (`auth-shop.json`, written by `setup_login.py`): load the storage
state, open `https://www.wegmans.com/`, harvest the Bearer off the first
`api.digitaldevelopment.wegmans.cloud` request (~2.5s headless). The shop
MSAL client requests `offline_access`, so its cache holds a refresh token
and silent renewal keeps working long-term — unlike Meals2Go.

**The shop token is accepted by BOTH backends** (verified live): the
commerce API *and* wegapi (Meals2Go cart, with the APIM key). Shared
audience, superset scopes. A wegmans.com login can therefore power the
entire server; the reverse is untested (the Meals2Go token's scopes lack
`InstacartConnect.*` / `Commerce.*`, and it was expired at capture time).

Other authenticated commerce endpoints observed:

- `GET /users/profile?api-version=2023-05-18`
- `GET /commerce/account/addresses?api-version=2024-03-06-preview`
- `GET /commerce/order/orders/activeorders?api-version=2024-03-04-preview`
- `GET /commerce/saved-list/savedlists?api-version=2024-02-20-preview`
- `GET /commerce/my-items?api-version=2024-01-26`
- `GET /commerce/browse/products/?productid=...&storeNumber=91&api-version=2023-09-22`
  — batch product detail (server-side alternative to Algolia)
- `POST /commerce/instacart/fulfillment/service_options/pickup?api-version=2023-11-13-preview`
  — pickup windows/fees, body `{cart_total_cents, items_count, location_code}`
- `POST /commerce/signalr/orders/negotiate?api-version=2024-03-18-preview`
- `POST /cooklist/v2/graphql` — recipes/cooking content

## Constants observed in this session

| Name | Value |
|---|---|
| Store ID (Fairfax) | 16 |
| Storefront ID | 1 |
| Organization ID | 1 |
| APIM Subscription-Key | `5197901a4fb04988a35800505266ef1c` |
| Loyalty Number | `<LOYALTY_ID>` |
| Customer ID | `<CUSTOMER_ID>` |

## Open questions

- How does updating modifiers on an existing cart item work — is it PATCH with new `payload`, or is `cartItemId` immutable so it's a remove + add?
- Does PATCH support partial payloads or must it always echo the full kit?
- Is there a `/cart-items/{cartItemId}` per-item endpoint?
- How does "group order" mode differ?
- Is the menu cacheable per store, or do prices change per-request?
