# Recipes for common Wegmans orders

Worked examples for cases that have tripped up fresh LLM sessions.
Each shows the `selections` dict you'd pass to `add_to_cart` after
fetching `get_item_details(kit_id)`.

## Large Chicken Tender Sub on Sesame, Buffalo style, with everything

```python
kit_id = 458
selections = {
    884:  {408: 1},                 # Size: Large
    672:  {1022: 1},                 # Bread: Sesame
    723:  {1076: 1},                 # Style: Buffalo
    669:  {4133: 1},                 # Toasting: "Toast roll with protein & cheese only" (default)
    778:  {1580: 1},                 # Slicing: "Cut in 4 Pieces" (default)
    666:  {1271: 1},                 # Cheese: Pepper Jack (spiciest)
    670:  {1027: 1, 1028: 1, 1030: 1, 1270: 1, 1031: 1, 1032: 1, 1035: 1, 1037: 1},
                                     # Condiments: all 8 free
    7025: {1036: 1, 1033: 1, 1034: 1},
                                     # Sauces: Buffalo + Ranch + Blue Cheese (max=3)
    671:  {1038: 1, 1039: 1, 1040: 1, 1041: 1, 1042: 1, 1043: 1,
           1044: 1, 1045: 1, 1046: 1},
                                     # Toppings: 9 of 12 (max=9)
}
```

For "Extra" amount on a topping:

```python
671: {1043: {"quantity": 1, "attribute": "Extra"}}  # Hot Banana Peppers extra
```

These kit_content_ids and item_ids may differ per store. **Always
re-fetch with `get_item_details(458)` against the current
`store_id`** — don't hardcode.

## Chicken Tender Wrap (Spinach), spiciest possible

Only one Size (Whole Wrap, sub-kit 1155). Style: Buffalo (1076).
Spinach wrap = item 1026. Add all free condiments/sauces/toppings,
Pepper Jack cheese, optionally Hot Banana Peppers Extra.

```python
kit_id = 1154
selections = {
    3329: {1155: 1},                 # Size: Whole Wrap
    3334: {1026: 1},                 # Wrap: Spinach
    3337: {1076: 1},                 # Style: Buffalo
    3330: {1271: 1},                 # Cheese: Pepper Jack
    3332: {1579: 1},                 # Slicing: Cut In 2 Pieces
    7039: {1036: 1, 1033: 1, 1034: 1},
    3335: {1027: 1, 1028: 1, 1030: 1, 1270: 1, 1031: 1, 1032: 1,
           1035: 1, 4880: 1, 4881: 1},
    3336: {1038: 1, 1039: 1, 1040: 1, 1041: 1, 1042: 1, 1043: 1,
           1044: 1, 1045: 1, 1046: 1},
}
```

## 20 Jumbo Chicken Wings (flat kit, no sub-kits)

```python
kit_id = 3003
selections = {
    10194: {7159: 1, 366: 1},         # Sauces: Sweet Buffalo + Plain (max=2)
    10193: {372: 1, 373: 1},          # Dipping sauce: Ranch + Blue Cheese (min=2, free up to 2)
}
```

## Switching fulfillment to a different store

```python
# 1. find the store
stores = await search_stores(near="14216", fulfillment_type="curbside")
# pick the right one (e.g. Amherst St., store_id 91)

# 2. set context
await set_fulfillment(store_id=91, fulfillment_type="curbside")

# 3. subsequent add_to_cart / view_cart use that context
```

## Clipping every available coupon

```python
# shop side (the big set, ~100):
await clip_coupons()  # source="shop" default

# meals2go side (smaller, requires WEGMANS_LOYALTY_ID):
await clip_coupons(source="meals2go")
```
