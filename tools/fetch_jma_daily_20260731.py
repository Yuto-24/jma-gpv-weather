from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import fitz
import requests
from PIL import Image

RUN_DATE = "2026-07-31"
SOURCE_DATE = "2026-07-30"
SOURCE_COMPACT = "20260730"
OUT = Path("jma-source-20260731")
ORIG = OUT / "originals"
ORIG.mkdir(parents=True, exist_ok=True)

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 JMA weather archive workflow"})


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, path: Path, required_type: str | None = None) -> dict:
    r = S.get(url, timeout=60)
    print("GET", r.status_code, len(r.content), r.headers.get("content-type"), url)
    r.raise_for_status()
    if required_type and required_type not in (r.headers.get("content-type") or ""):
        raise RuntimeError(f"Unexpected content type for {url}: {r.headers.get('content-type')}")
    path.write_bytes(r.content)
    return {
        "url": url,
        "content_type": r.headers.get("content-type"),
        "last_modified": r.headers.get("last-modified"),
        "etag": r.headers.get("etag"),
    }


def pdf_text(path: Path) -> str:
    with fitz.open(path) as doc:
        return "\n".join(page.get_text() for page in doc)


def pdf_info(path: Path) -> tuple[int, list[list[float]]]:
    with fitz.open(path) as doc:
        return doc.page_count, [[p.rect.width, p.rect.height] for p in doc]


def image_to_pdf(image: Path, pdf: Path) -> None:
    with Image.open(image) as im:
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, "white")
            if "A" in im.getbands():
                bg.paste(im, mask=im.getchannel("A"))
            else:
                bg.paste(im)
            im = bg
        else:
            im = im.convert("RGB")
        im.save(pdf, "PDF", resolution=200.0)


def select_filename(values: list[str], product: str, cycle: str) -> str:
    target = f"_{SOURCE_COMPACT}{cycle}0000_MET_CHT_JCI{product.lower()}_"
    matches = [v for v in values if target in v]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {product} {cycle} file, got {matches}")
    return matches[0]


items: list[dict] = []
fixed = [
    (1, "AUPQ35", "aupq35_12.pdf"),
    (2, "AUPQ78", "aupq78_12.pdf"),
    (4, "FXJP854", "fxjp854_12.pdf"),
    (5, "AXFE578", "axfe578_12.pdf"),
    (7, "FXFE5782", "fxfe5782_12.pdf"),
    (8, "FXFE502", "fxfe502_12.pdf"),
]
for ordinal, product, filename in fixed:
    url = f"https://www.jma.go.jp/bosai/numericmap/data/nwpmap/{filename}"
    path = ORIG / f"{ordinal:02d}_{product}_{SOURCE_COMPACT}_12.pdf"
    headers = fetch(url, path, "application/pdf")
    text = pdf_text(path)
    print(product, text[-500:])
    if not re.search(r"301200UTC\s+JUL\s+2026", text, re.I):
        raise RuntimeError(f"{product} cycle/date validation failed: {text[-1000:]}")
    pages, dims = pdf_info(path)
    if pages != 1:
        raise RuntimeError(f"{product}: expected one page, got {pages}")
    items.append({
        "ordinal": ordinal,
        "product_code": product,
        "cycle_utc": "12",
        "source_date_utc": SOURCE_DATE,
        "issue_time_utc": headers.get("last_modified"),
        "valid_time_utc": f"{SOURCE_DATE}T12:00:00Z",
        "official_source_url": url,
        "retrieval_time_utc": datetime.now(timezone.utc).isoformat(),
        "original_file": str(path.relative_to(OUT)),
        "normalized_pdf": str(path.relative_to(OUT)),
        "mime_type": "application/pdf",
        "page_count": pages,
        "page_dimensions": dims,
        "byte_size": path.stat().st_size,
        "sha256": sha256(path),
        "headers": headers,
    })

list_url = "https://www.jma.go.jp/bosai/weather_map/data/list.json"
r = S.get(list_url, timeout=60)
print("GET", r.status_code, len(r.content), r.headers.get("content-type"), list_url)
r.raise_for_status()
listing = r.json()
(OUT / "weather_map_list.json").write_text(json.dumps(listing, ensure_ascii=False, indent=2), encoding="utf-8")

surface_specs = [
    (3, "ASAS", "12", listing["asia_monochrome"]["now"]),
    (6, "ASAS", "18", listing["asia_monochrome"]["now"]),
    (9, "FSAS24", "12", listing["asia_monochrome"]["ft24"]),
]
for ordinal, product, cycle, values in surface_specs:
    source_name = select_filename(values, "asas" if product == "ASAS" else "fsas24", cycle)
    png_url = f"https://www.jma.go.jp/bosai/weather_map/data/png/{source_name}"
    png_path = ORIG / f"{ordinal:02d}_{product}_{SOURCE_COMPACT}_{cycle}.png"
    headers = fetch(png_url, png_path, "image/png")
    pdf_path = ORIG / f"{ordinal:02d}_{product}_{SOURCE_COMPACT}_{cycle}.pdf"
    image_to_pdf(png_path, pdf_path)
    pages, dims = pdf_info(pdf_path)
    items.append({
        "ordinal": ordinal,
        "product_code": product,
        "cycle_utc": cycle,
        "source_date_utc": SOURCE_DATE,
        "issue_time_utc": source_name.split("_", 1)[0],
        "valid_time_utc": f"{SOURCE_DATE}T{cycle}:00:00Z" if product == "ASAS" else f"base {SOURCE_DATE}T{cycle}:00:00Z; +24 h",
        "official_source_url": png_url,
        "retrieval_time_utc": datetime.now(timezone.utc).isoformat(),
        "original_file": str(png_path.relative_to(OUT)),
        "normalized_pdf": str(pdf_path.relative_to(OUT)),
        "mime_type": "image/png",
        "page_count": pages,
        "page_dimensions": dims,
        "byte_size": png_path.stat().st_size,
        "sha256": sha256(png_path),
        "normalized_pdf_sha256": sha256(pdf_path),
        "source_filename": source_name,
        "headers": headers,
    })

items.sort(key=lambda x: x["ordinal"])
manifest = {
    "run_date_jst": RUN_DATE,
    "source_date_utc": SOURCE_DATE,
    "retrieval_time_utc": datetime.now(timezone.utc).isoformat(),
    "items": items,
}
(OUT / f"{RUN_DATE}_JMA_WeatherCharts_SourceManifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(manifest, ensure_ascii=False, indent=2))
