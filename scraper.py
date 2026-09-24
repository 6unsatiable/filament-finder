"""Fetch filament listings from Shopify stores via their public /products.json feed."""
import json
import unicodedata
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (FilamentFinder price comparison)"
MAX_PAGES = 20

MATERIALS = [  # order matters: first match wins
    ("PETG", r"\bpetg\b|\bpet-g\b"),
    ("PLA", r"\bpla\b|\bpla\+|\bpla-|pla\s*(pro|plus|max)"),
    ("ABS", r"\babs\b|\babs\+"),
    ("ASA", r"\basa\b"),
    ("TPU", r"\btpu\b|\btpe\b|flexible"),
    ("Nylon", r"\bnylon\b|\bpa6|\bpa12|\bpa-?cf|\bpaht"),
    ("PC", r"\bpc\b|polycarbonate"),
    ("PP", r"\bpp\b|polypropylene"),
    ("PVA", r"\bpva\b|\bbvoh\b"),
    ("HIPS", r"\bhips\b"),
    ("PET", r"\bpet\b|\bpctg\b"),
    ("PEEK/PEI", r"\bpeek\b|\bpei\b|\bultem\b|\bpekk\b"),
]
FEATURES = {
    "Silk": r"\bsilk",
    "Matte": r"\bmatte",
    "Carbon fiber": r"carbon\s*fib|\b-?cf\b|\bcf\d*\b",
    "High speed": r"high[\s-]*speed|\bhs\b|hyper|rapid",
    "Glow": r"\bglow",
    "Wood": r"\bwood",
    "Marble": r"\bmarble",
    "Rainbow/Multicolor": r"rainbow|multi[\s-]*colou?r|dual[\s-]*colou?r|tri[\s-]*colou?r|gradient",
    "Plus/Pro": r"pla\s*\+|pla\s*(pro|plus)|\babs\+",
    "Refill (no spool)": r"refill|spool-?less|no spool",
}
NOT_FILAMENT = re.compile(
    r"resin|printer\b(?!.*filament)|nozzle|hotend|build plate|\bbed\b|dryer|storage box|"
    r"gift card|sample pack|swatch|spare part|\bmotor|extruder|\bkit\b(?!.*filament)",
    re.I,
)
HARD_EXCLUDE = re.compile(r"pellet|granule|mystery|cutter|assembly|swatch|\bcord\b|cleaning|protection kit|respooler|winder|roller|whiteboard|module|adapter|pei plate|build plate|\bplate\b|track switch|funnel|filament guide|sensor|buffer|\bhub\b|spool holder|nozzle|hotend|gift card|\bdryer\b", re.I)
REGION_CODES = {"eu", "uk", "au", "ca", "de", "jp", "europe", "canada", "australia", "united kingdom", "other"}
REGION_BAD = re.compile(r"ship to (?!usa|us\b|united states)|\b(eu|uk|au|ca|europe|canada|australia|de|jp) only\b|pre-?sale|\bmoq\b|\b(eu|uk|au|ca|europe|canada|australia) (warehouse|stock)", re.I)


def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_text(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xml"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_bambu(store):
    """Bambu Lab's store isn't Shopify: read filament pages from the sitemap and parse
    the schema.org ProductGroup JSON embedded in each page's Next.js payload."""
    base = store["url"]
    slugs = re.findall(r"/products/([a-z0-9-]+)</loc>", fetch_text(f"{base}/sitemap_products_1.xml"))
    slugs = [s for s in dict.fromkeys(slugs)
             if detect_material(s.replace("-", " ")) or "filament" in s or "support-for" in s]

    def page(slug):
        try:
            html = fetch_text(f"{base}/products/{slug}")
        except Exception:  # noqa: BLE001
            return None
        chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
        flight = "".join(json.loads(f'"{c}"') for c in chunks) + html
        m = re.search(r'\{\s*"@context":\s*"https?://schema\.org/?",\s*"@type":\s*"Product(Group)?"', flight)
        if not m:
            return None
        try:
            d = json.JSONDecoder().raw_decode(flight[m.start():])[0]
        except ValueError:
            return None
        variants = d.get("hasVariant") or [d]
        name = d.get("name", slug)
        out_vars = []
        for v in variants:
            offer = v.get("offers") or {}
            if isinstance(offer, list):
                offer = offer[0] if offer else {}
            if offer.get("priceCurrency", "USD") != "USD" or offer.get("price") is None:
                continue
            vname = v.get("name", "")
            if vname.startswith(name):
                vname = vname[len(name):].lstrip(" -")
            out_vars.append({
                "id": v.get("sku") or vname, "title": vname or "Default Title",
                "price": str(offer["price"]), "compare_at_price": None,
                "available": "InStock" in str(offer.get("availability", "")),
                "featured_image": {"src": v["image"]} if isinstance(v.get("image"), str) else None,
                "url": offer.get("url"),
            })
        return {"id": d.get("productGroupID") or slug, "title": name, "handle": slug,
                "product_type": "Filament", "tags": [], "options": [], "images": [],
                "variants": out_vars}

    with ThreadPoolExecutor(max_workers=4) as ex:
        return [p for p in ex.map(page, slugs) if p and p["variants"]]


def fetch_store(store):
    if store.get("type") == "bambu":
        return fetch_bambu(store)
    products = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{store['url']}/products.json?limit=250&page={page}"
        for attempt in range(3):
            try:
                batch = fetch_json(url).get("products", [])
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise
        products.extend(batch)
        if len(batch) < 250:
            break
    return products


def parse_weight_kg(text):
    """Return total filament weight in kg from free text, or None."""
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"(\d),(\d)", r"\1.\2", t)
    # "4 x 1kg", "2*1kg", "1kg x 3"
    m = re.search(r"(\d+)\s*[x×*]\s*(\d+(?:\.\d+)?)\s*(kg|g)\b", t) or None
    if m:
        n, w, u = int(m.group(1)), float(m.group(2)), m.group(3)
        return n * (w if u == "kg" else w / 1000)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|g)\s*[x×*]\s*(\d+)\b", t)
    if m:
        w, u, n = float(m.group(1)), m.group(2), int(m.group(3))
        return n * (w if u == "kg" else w / 1000)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|kilo|g|gram|grams|lb|lbs)\b", t)
    if not m:
        return None
    w, u = float(m.group(1)), m.group(2)
    kg = w if u in ("kg", "kilo") else w * 0.4536 if u.startswith("lb") else w / 1000
    if not 0.05 <= kg <= 50:
        return None
    pack = re.search(r"(\d+)\s*(?:-\s*)?(?:rolls?|spools?|pack|pcs|pieces)\b|\bpack of (\d+)|bundle of (\d+)", t)
    if pack:
        n = int(next(g for g in pack.groups() if g))
        # only multiply if the weight looks per-spool (not already the total)
        if 1 < n <= 20 and kg <= 3:
            kg *= n
    return round(kg, 3)


def parse_diameter(text):
    t = text.lower()
    if re.search(r"2\.85|3\.0\s*mm|\b3mm\b", t):
        return "2.85"
    return "1.75"


def detect_material(text):
    t = text.lower()
    for name, pat in MATERIALS:
        if re.search(pat, t):
            return name
    return None


def build_listings(store, products):
    out = []
    for p in products:
        tags = p.get("tags") or []
        if isinstance(tags, str):
            tags = tags.split(",")
        ptext = " ".join([p.get("title", ""), p.get("product_type", ""), " ".join(tags)])
        if "filament" not in ptext.lower() and not detect_material(p.get("title", "")):
            continue
        if NOT_FILAMENT.search(p.get("title", "")) and "filament" not in p.get("title", "").lower():
            continue
        title = p.get("title", "")
        if HARD_EXCLUDE.search(title):
            continue
        if " + " in title and not detect_material(title.split(" + ")[0]) and "filament" not in title.split(" + ")[0].lower():
            continue  # printer + filament bundles
        material_p = detect_material(p.get("title", "")) or detect_material(ptext)
        images = p.get("images") or []
        default_img = images[0]["src"] if images else None
        img_by_id = {i["id"]: i["src"] for i in images}
        for v in p.get("variants", []):
            vt = v.get("title") or ""
            if any(seg.strip().lower() in REGION_CODES for seg in re.split(r"[/|]", vt)):
                continue
            if REGION_BAD.search(vt) or REGION_BAD.search(p.get("title", "")):
                continue
            try:
                price = float(v.get("price") or 0)
            except ValueError:
                continue
            if price <= 0:
                continue
            full = f"{p.get('title', '')} {vt}"
            weight = parse_weight_kg(vt) or parse_weight_kg(p.get("title", "")) or parse_weight_kg(p.get("body_html") or "")
            material = detect_material(vt) or material_p
            if weight and not 4 <= price / weight <= 1500:  # implausible $/kg -> bad weight parse
                weight = None
            if not material and "filament" in p.get("title", "").lower():
                material = "PLA" if re.search(r"silk|matte|marble|wood", full, re.I) else "Other"
            if not material:
                continue
            feats = [f for f, pat in FEATURES.items() if re.search(pat, full, re.I)]
            compare = v.get("compare_at_price")
            try:
                compare = float(compare) if compare else None
            except ValueError:
                compare = None
            color = next(
                (v.get(f"option{i+1}") for i, o in enumerate(p.get("options", [])[:3])
                 if isinstance(o, dict) and re.search(r"colou?r", o.get("name", ""), re.I)),
                None,
            ) or (vt if vt.lower() != "default title" else "")
            fi = v.get("featured_image")
            out.append({
                "id": f"{store['name']}:{v['id']}",
                "store": store["name"],
                "product": p.get("title", ""),
                "product_id": f"{store['name']}:{p['id']}",
                "variant": "" if vt.lower() == "default title" else vt,
                "color": color,
                "url": v.get("url") or f"{store['url']}/products/{p['handle']}?variant={v['id']}",
                "image": (fi or {}).get("src") or img_by_id.get(v.get("image_id")) or default_img,
                "price": price,
                "compare_at": compare if compare and compare > price else None,
                "available": bool(v.get("available")),
                "material": material,
                "features": feats,
                "diameter": parse_diameter(full),
                "weight_kg": weight,
                "price_per_kg": round(price / weight, 2) if weight else None,
            })
    return out


def scrape_all(stores, log=print):
    listings, status = [], {}

    def job(s):
        t0 = time.time()
        try:
            prods = fetch_store(s)
            items = build_listings(s, prods)
            return s, items, {"ok": True, "products": len(prods), "listings": len(items),
                              "seconds": round(time.time() - t0, 1)}
        except Exception as e:  # noqa: BLE001
            return s, [], {"ok": False, "error": str(e)[:200]}

    with ThreadPoolExecutor(max_workers=8) as ex:
        for s, items, st in ex.map(job, stores):
            listings.extend(items)
            status[s["name"]] = st
            log(f"[scrape] {s['name']}: {st}")
    return listings, status
