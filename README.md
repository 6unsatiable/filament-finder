# Filament Finder

Compares 3D printer filament prices across ~16 online stores, with filters and $/kg sorting.
Pure Python standard library — no dependencies.

```
python3 server.py            # http://localhost:8347
python3 server.py --port 8080 --refresh-hours 12
```

- Prices come from each store's public Shopify `/products.json` feed, refreshed every 6h (or via "Refresh prices").
- Results are cached in `cache.json` so restarts are instant.
- Add/remove stores in `stores.json` (any Shopify store works).
- Filters: material, diameter, price, spool weight, features (silk/matte/CF/high-speed…), store, in-stock, search.
- Sorting: $/kg, price, discount, weight, name. Filters are kept in the URL, so you can bookmark searches.

API: `GET /api/listings?material=PLA,PETG&diameter=1.75&in_stock=1&sort=price_per_kg&order=asc&group=1`,
`GET /api/facets`, `POST /api/refresh`.

Notes: material/weight/diameter are parsed from product text, so a few may be wrong or missing
(unlabelled diameter is assumed 1.75 mm). Prices are in each store's base currency (USD for all defaults)
and exclude shipping.
