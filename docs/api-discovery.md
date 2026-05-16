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
