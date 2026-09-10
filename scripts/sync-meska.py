#!/usr/bin/env python3
"""
Meska szinkron a Gem Bandit weboldalhoz.

Lekéri a Meska-bolt AKTÍV termékeit (POST /Search/api), és újraírja a
site/index.html két jelölt blokkját:

  <!-- PRODUCTS:START --> … <!-- PRODUCTS:END -->   hat termékkártya
  <!-- STATS:START -->    … <!-- STATS:END -->      bizalmi sáv (értékelés, darabszám)

Kiemelt darabok: site/featured.json (id + kézzel írt leírás). Ami ebből már nem
kapható a Meskán (elkelt, törölt, fenntartott), automatikusan kiesik, a helyére
a legújabb kapható termék kerül. A képeket a Meska nagy méretű változatából
tölti le és 900 px-re kicsinyíti (site/images/p-<id>.jpg).

Futtatás:  python3 scripts/sync-meska.py            (a repo gyökeréből)
           python3 scripts/sync-meska.py --dry-run  (nem ír semmit, csak kiírja)

Nincs külső függőség. A képkicsinyítéshez a Pillow (pip install pillow) vagy
macOS-en a beépített `sips` kell; ha egyik sincs, az eredeti méret marad.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import os

ROOT = Path(__file__).resolve().parent.parent
SITE = Path(os.environ.get("SITE_DIR", ROOT / "site")).resolve()   # a publikus repóban SITE_DIR=. (a site a gyökérben van)
INDEX = SITE / "index.html"
FEATURED = SITE / "featured.json"
IMG_DIR = SITE / "images"

SHOP_ID = 53206
SHOP_URL = "https://www.meska.hu/shop/Gembandit"
API_URL = "https://www.meska.hu/Search/api"
CARD_COUNT = 6
IMG_MAX = 900
RESERVED_MARKERS = ("megrendelésére készült", "csak számára lehetséges")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 gembandit-sync/1.1")

DRY = "--dry-run" in sys.argv


# ----------------------------------------------------------------------------
# HTTP (csak stdlib)
# ----------------------------------------------------------------------------

def http(url: str, data: bytes | None = None, headers: dict | None = None,
         timeout: int = 40) -> bytes:
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ----------------------------------------------------------------------------
# Meska
# ----------------------------------------------------------------------------

def fetch_products() -> tuple[list[dict], int]:
    """Az összes aktív termék (a Meska csak az aktívakat adja vissza)."""
    products: list[dict] = []
    total = 0
    page = 1
    while True:
        body = json.dumps({"p": page, "sort": "default", "shop_id": SHOP_ID,
                           "productType": []}).encode()
        raw = http(API_URL, data=body, headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest", "Referer": SHOP_URL})
        data = json.loads(raw)
        total = int(data.get("total") or 0)
        batch = data.get("products") or []
        products.extend(batch)
        if not batch or len(products) >= total or page > 10:
            break
        page += 1
    return products, total


def fetch_rating() -> tuple[str | None, int | None]:
    """Értékelés a bolt oldalának JSON-LD blokkjából. Ha nincs, None."""
    try:
        html = http(SHOP_URL).decode("utf-8", "ignore")
    except (urllib.error.URLError, TimeoutError):
        return None, None
    m_val = re.search(r'"ratingValue"\s*:\s*"?(\d+(?:[.,]\d+)?)', html)
    m_cnt = re.search(r'"(?:reviewCount|ratingCount)"\s*:\s*"?(\d+)', html)
    val = m_val.group(1).replace(".", ",") if m_val else None
    if val and "," not in val:
        val = f"{val},0"
    cnt = int(m_cnt.group(1)) if m_cnt else None
    return val, cnt


def is_sellable(p: dict) -> bool:
    """Csak ami a lista szerint megvehető: aktív, készleten, nem rendelésre."""
    if not p.get("is_active", True) or not p.get("in_stock", True):
        return False
    if p.get("specific_customer_id"):
        return False
    if str(p.get("to_order_product") or "0") == "1":
        return False
    return True


def is_reserved_on_page(p: dict) -> bool:
    """A lista-API nem jelöli, ha egy darab egy vevőnek van fenntartva;
    ezt csak a termékoldal szövege mutatja. Csak a kiválasztottakra hívjuk."""
    try:
        html = http(p["formatted_url"]).decode("utf-8", "ignore").lower()
    except (urllib.error.URLError, TimeoutError):
        return False
    return any(m in html for m in RESERVED_MARKERS)


# ----------------------------------------------------------------------------
# Kártya-szöveg a termékadatokból (csak tények)
# ----------------------------------------------------------------------------

TYPE_BY_CATEGORY = [
    ("gyűrű", "Gyűrű"),
    ("bross", "Bross"),
    ("kitűző", "Kitűző"),
    ("fülbevaló", "Fülbevaló"),
    ("karkötő", "Karkötő"),
    ("karperec", "Karperec"),
    ("nyaklánc", "Medál"),
    ("medál", "Medál"),
]

MATERIAL_PATTERNS = [
    (r"orvosi\s+ac[ée]l", "orvosi acél"),
    (r"nemesac[ée]l", "nemesacél"),
    (r"aranyoz", "aranyozott"),
    (r"ez[üu]st[öo]z", "ezüstözött"),
    (r"bronz", "bronz"),
    (r"r[ée]z\b", "réz"),
    (r"zom[áa]nc", "zománcozott"),
    (r"[áa]ll[íi]that[óo]", "állítható"),
    (r"l[áa]nccal", "lánccal"),
    (r"d[íi]szdoboz|d[íi]szcsomagol", "díszdobozban"),
]


def product_type(p: dict) -> str:
    text = f"{p.get('category_string','')} {p.get('product_name','')}".lower()
    for needle, label in TYPE_BY_CATEGORY:
        if needle in text:
            return label
    return "Ékszer"


def short_desc(p: dict) -> str:
    """Rövid, tényszerű sor: típus, anyag, méret. Semmi marketing."""
    desc = (p.get("product_description") or "").replace("​", "")
    low = desc.lower()
    bits: list[str] = []
    seen = set()
    for pat, label in MATERIAL_PATTERNS:
        if re.search(pat, low) and label not in seen:
            bits.append(label)
            seen.add(label)
    size = re.search(r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(cm|mm)", low)
    size_txt = f"{size.group(1)}×{size.group(2)} {size.group(3)}".replace(".", ",") if size else ""
    parts = [product_type(p)]
    if bits:
        parts.append(", ".join(bits[:4]))
    if size_txt:
        parts.append(size_txt)
    return " · ".join(parts) + "."


# ----------------------------------------------------------------------------
# Képek
# ----------------------------------------------------------------------------

CROP_BOTTOM = 0.10   # a Meska a bal alsó sarokba vízjelet tesz; az alsó 10% levágásával eltűnik


def shrink_image(path: Path) -> None:
    """Alsó sáv levágása (vízjel), kicsinyítés. Pillow, ha van; különben macOS sips;
    különben az eredeti marad."""
    try:
        from PIL import Image  # type: ignore
        im = Image.open(path).convert("RGB")
        w, h = im.size
        im = im.crop((0, 0, w, int(h * (1 - CROP_BOTTOM))))
        im.thumbnail((IMG_MAX, IMG_MAX))
        im.save(path, "JPEG", quality=82, optimize=True)
        return
    except ImportError:
        pass
    except Exception as e:  # pragma: no cover
        print(f"  ! Pillow nem tudta feldolgozni ({e})")
    if shutil.which("sips"):
        try:
            out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                                 capture_output=True, text=True, check=False).stdout
            w = int(re.search(r"pixelWidth:\s*(\d+)", out).group(1))
            h = int(re.search(r"pixelHeight:\s*(\d+)", out).group(1))
            subprocess.run(["sips", "-c", str(int(h * (1 - CROP_BOTTOM))), str(w),
                            "--cropOffset", "0", "0", str(path), "--out", str(path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except (AttributeError, ValueError):
            pass
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "82",
                        "-Z", str(IMG_MAX), str(path), "--out", str(path)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def download_image(p: dict) -> str:
    """Letölti (ha még nincs) a termékfotót; visszaadja a relatív útvonalat."""
    pid = str(p["id"])
    out = IMG_DIR / f"p-{pid}.jpg"
    if out.exists():
        return f"images/{out.name}"
    small = p.get("image_url") or ""
    if not small:
        return ""
    if DRY:
        print(f"  [dry] kép letöltése kimarad: {small}")
        return f"images/{out.name}"
    content = b""
    for src in (small.replace("/small/", "/large/"), small):
        try:
            content = http(src, timeout=60)
        except (urllib.error.URLError, TimeoutError):
            content = b""
        if len(content) >= 10_000:
            break
    if not content:
        print(f"  ! nem jött le a kép: {small}")
        return ""
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    out.write_bytes(content)
    shrink_image(out)
    return f"images/{out.name}"


# ----------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------

def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def card_html(p: dict, desc: str, img: str) -> str:
    """Egy kitűzött kártya a falon (site/styles.css .card): ragasztószalag, kép, kézírásos árcédula, név, leírás, Meska-link."""
    name = esc(p["product_name"].strip())
    return f"""
          <a class="card" href="{esc(p['formatted_url'])}" target="_blank" rel="noopener">
            <span class="tape" aria-hidden="true"></span>
            <div class="card-img">
              <img src="{img}" alt="" width="900" height="810" loading="lazy" decoding="async">
              <span class="price-tag">{esc(p['formatted_price'])}</span>
            </div>
            <div class="card-body">
              <h3>{name}</h3>
              <p>{esc(desc)}</p>
            </div>
            <span class="buy">Megnézem a Meskán <span class="arrow">→</span></span>
          </a>
"""


def stats_html(rating: str | None, reviews: int | None, active: int, sold: int | None) -> str:
    """A hero alatti sor. Az értékelés-számot szándékosan nem írjuk ki (a tulaj kérése: kevésnek tűnik)."""
    parts = [f"<li><b>{active}</b> darab kapható</li>"]
    if sold:
        parts.append(f"<li><b>{sold}</b> darab már elkelt</li>")
    parts.append('<li><a href="#egyedi">egyedi darabot is kérhetsz →</a></li>')
    inner = "\n            ".join(parts)
    return f"""
          <ul class="stats" aria-label="A Meska-bolt adatai">
            {inner}
          </ul>
"""


def replace_block(html: str, name: str, new_inner: str) -> str:
    start = f"<!-- {name}:START"
    end = f"<!-- {name}:END -->"
    i = html.find(start)
    j = html.find(end)
    if i < 0 or j < 0:
        sys.exit(f"Nem találom a(z) {name} jelölőket az index.html-ben.")
    line_end = html.find("-->", i) + 3
    return html[:line_end] + new_inner + "        " + html[j:]


# ----------------------------------------------------------------------------

def main() -> int:
    products, total = fetch_products()
    sellable = [p for p in products if is_sellable(p)]
    print(f"Meska: {total} aktív termék, ebből a lista szerint megvehető: {len(sellable)}")
    if len(sellable) < 3:
        sys.exit("Túl kevés termék jött vissza, valami nem stimmel. Nem írok semmit.")

    by_id = {str(p["id"]): p for p in sellable}

    featured = json.loads(FEATURED.read_text(encoding="utf-8")) if FEATURED.exists() else []
    chosen: list[tuple[dict, str | None]] = []
    for f in featured:
        p = by_id.get(str(f.get("id")))
        if not p:
            print(f"  kiesett (már nem kapható): {f.get('id')} {f.get('name', '')}")
            continue
        if is_reserved_on_page(p):
            print(f"  kiesett (egy vevőnek fenntartva): {p['id']} {p['product_name'][:50]}")
            continue
        chosen.append((p, f.get("desc")))

    if len(chosen) < CARD_COUNT:
        used = {str(p["id"]) for p, _ in chosen}
        newest = sorted(sellable, key=lambda p: p.get("create_date", ""), reverse=True)
        for p in newest:
            if str(p["id"]) in used:
                continue
            if is_reserved_on_page(p):
                continue
            chosen.append((p, None))
            used.add(str(p["id"]))
            print(f"  beugró: {p['id']} {p['product_name'][:50]}")
            if len(chosen) >= CARD_COUNT:
                break
    chosen = chosen[:CARD_COUNT]

    cards = []
    for p, desc in chosen:
        img = download_image(p)
        cards.append(card_html(p, desc or short_desc(p), img))
    # a .grid nyitó/záró div a jelölőkön KÍVÜL van az index.html-ben (a rács végén egy kézi poszt-it is ül)
    products_block = "\n" + "".join(cards) + "\n"


    rating, reviews = fetch_rating()
    # Az eladott darabszám bejelentkezés nélkül nem kérdezhető le; kézzel tartjuk karban.
    html = INDEX.read_text(encoding="utf-8")
    m_sold = re.search(r"<b>(\d+)</b> darab már elkelt", html)
    sold = int(m_sold.group(1)) if m_sold else None
    stats_block = stats_html(rating, reviews, len(sellable), sold)

    new_html = replace_block(html, "PRODUCTS", products_block)
    new_html = replace_block(new_html, "STATS", stats_block)

    if new_html == html:
        print("Nincs változás.")
        return 0
    if DRY:
        print("[dry-run] változna az index.html, de nem írom.")
        return 0
    INDEX.write_text(new_html, encoding="utf-8")
    print("index.html frissítve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
