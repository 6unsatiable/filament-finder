"""Filament Finder: compare 3D printer filament prices across stores.

Run:  python3 server.py [--port 8000] [--refresh-hours 6]
"""
import argparse
import json
import os
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from scraper import scrape_all

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache.json")
STATE = {"listings": [], "status": {}, "updated": None, "refreshing": False}
LOCK = threading.Lock()


def load_stores():
    with open(os.path.join(HERE, "stores.json")) as f:
        return json.load(f)


def refresh():
    with LOCK:
        if STATE["refreshing"]:
            return False
        STATE["refreshing"] = True
    try:
        listings, status = scrape_all(load_stores())
        with LOCK:
            STATE.update(listings=listings, status=status, updated=time.time())
        with open(CACHE, "w") as f:
            json.dump({k: STATE[k] for k in ("listings", "status", "updated")}, f)
    finally:
        STATE["refreshing"] = False
    return True


def refresh_loop(hours):
    while True:
        if not STATE["updated"] or time.time() - STATE["updated"] > hours * 3600:
            refresh()
        time.sleep(60)


def query(params):
    g = lambda k, d="": params.get(k, [d])[0]  # noqa: E731
    multi = lambda k: [x for v in params.get(k, []) for x in v.split(",") if x]  # noqa: E731
    items = STATE["listings"]
    q = g("q").lower().split()
    materials, stores, feats = set(multi("material")), set(multi("store")), set(multi("feature"))
    diameter = g("diameter")
    in_stock = g("in_stock") == "1"
    need_weight = g("has_weight") == "1"
    min_p, max_p = float(g("min_price") or 0), float(g("max_price") or 1e9)
    min_w, max_w = float(g("min_weight") or 0), float(g("max_weight") or 1e9)

    res = []
    for it in items:
        if materials and it["material"] not in materials: continue
        if stores and it["store"] not in stores: continue
        if feats and not feats.issubset(it["features"]): continue
        if diameter and it["diameter"] != diameter: continue
        if in_stock and not it["available"]: continue
        if not (min_p <= it["price"] <= max_p): continue
        w = it["weight_kg"]
        if need_weight and not w: continue
        if (min_w or max_w < 1e9) and (not w or not (min_w <= w <= max_w)): continue
        if q:
            hay = f"{it['product']} {it['variant']} {it['store']} {it['color']}".lower()
            if not all(t in hay for t in q): continue
        res.append(it)

    sort, desc = g("sort", "price_per_kg"), g("order", "asc") == "desc"
    key = {
        "price": lambda x: x["price"],
        "price_per_kg": lambda x: x["price_per_kg"] if x["price_per_kg"] is not None else float("inf"),
        "discount": lambda x: -((x["compare_at"] or x["price"]) - x["price"]) / (x["compare_at"] or x["price"]),
        "weight": lambda x: x["weight_kg"] or 0,
        "name": lambda x: x["product"].lower(),
        "store": lambda x: (x["store"].lower(), x["price"]),
    }.get(sort, lambda x: x["price"])
    res.sort(key=key, reverse=desc)

    if g("group") == "1":  # keep cheapest-ranked variant per product
        seen, grouped = {}, []
        for it in res:
            if it["product_id"] not in seen:
                seen[it["product_id"]] = it.copy()
                seen[it["product_id"]]["variant_count"] = 0
                grouped.append(seen[it["product_id"]])
            seen[it["product_id"]]["variant_count"] += 1
        res = grouped

    offset, limit = int(g("offset") or 0), min(int(g("limit") or 60), 500)
    return {"total": len(res), "items": res[offset:offset + limit]}


def facets():
    count = lambda key: sorted(  # noqa: E731
        {v: sum(1 for i in STATE["listings"] if i[key] == v) for v in {i[key] for i in STATE["listings"]}}.items(),
        key=lambda kv: -kv[1])
    feats = {}
    for i in STATE["listings"]:
        for f in i["features"]:
            feats[f] = feats.get(f, 0) + 1
    return {
        "materials": count("material"),
        "stores": count("store"),
        "diameters": count("diameter"),
        "features": sorted(feats.items(), key=lambda kv: -kv[1]),
        "status": STATE["status"],
        "updated": STATE["updated"],
        "refreshing": STATE["refreshing"],
        "total": len(STATE["listings"]),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(HERE, "static"), **kw)

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/listings":
            try:
                return self.send_json(query(parse_qs(u.query)))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
        if u.path == "/api/facets":
            return self.send_json(facets())
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path == "/api/refresh":
            threading.Thread(target=refresh, daemon=True).start()
            return self.send_json({"started": not STATE["refreshing"]})
        self.send_error(404)

    def log_message(self, fmt, *args):
        if "/api/" not in (args[0] if args else ""):
            super().log_message(fmt, *args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--refresh-hours", type=float, default=6)
    args = ap.parse_args()
    if os.path.exists(CACHE):
        with open(CACHE) as f:
            STATE.update(json.load(f))
        print(f"Loaded {len(STATE['listings'])} cached listings")
    threading.Thread(target=refresh_loop, args=(args.refresh_hours,), daemon=True).start()
    print(f"Filament Finder on http://localhost:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
