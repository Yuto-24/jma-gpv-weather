from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import fitz
import requests
from PIL import Image

RUN_DATE = "2026-07-31"
SOURCE_DATE = "2026-07-30"
SOURCE_COMPACT = "20260730"
OUT = Path("jma-source-20260731")
ORIG = OUT / "originals"
PREV = OUT / "previews"
ORIG.mkdir(parents=True, exist_ok=True)
PREV.mkdir(parents=True, exist_ok=True)

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 JMA weather archive workflow"})


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, path: Path, required_prefix: str | None = None) -> dict[str, Any]:
    r = S.get(url, timeout=60)
    ctype = r.headers.get("content-type") or ""
    print("GET", r.status_code, len(r.content), ctype, url, flush=True)
    r.raise_for_status()
    if required_prefix and not ctype.startswith(required_prefix):
        raise RuntimeError(f"Unexpected content type for {url}: {ctype}")
    path.write_bytes(r.content)
    return {
        "url": url,
        "content_type": ctype,
        "last_modified": r.headers.get("last-modified"),
        "etag": r.headers.get("etag"),
        "date": r.headers.get("date"),
    }


def pdf_info(path: Path) -> tuple[int, list[list[float]]]:
    with fitz.open(path) as doc:
        return doc.page_count, [[p.rect.width, p.rect.height] for p in doc]


def render_pdf(path: Path, stem: str) -> list[str]:
    outputs: list[str] = []
    with fitz.open(path) as doc:
        for idx, page in enumerate(doc):
            pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), alpha=False)
            out = PREV / f"{stem}_p{idx + 1}.png"
            pix.save(out)
            outputs.append(str(out.relative_to(OUT)))
    return outputs


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


def all_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from all_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from all_strings(v)


def select_filename(strings: list[str], product: str, cycle: str) -> str:
    target = f"_{SOURCE_COMPACT}{cycle}0000_MET_CHT_JCI{product.lower()}_"
    matches = sorted({s for s in strings if target in s and (s.endswith(".png") or "_image" in s)})
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {product} {cycle} image, got {matches}")
    return matches[0].split("/")[-1]


items: list[dict[str, Any]] = []
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
    pages, dims = pdf_info(path)
    if pages != 1:
        raise RuntimeError(f"{product}: expected one page, got {pages}")
    previews = render_pdf(path, f"{ordinal:02d}_{product}_{SOURCE_COMPACT}_12")
    items.append({
        "ordinal": ordinal,
        "product_code": product,
        "cycle_utc": "12",
        "source_date_utc": SOURCE_DATE,
        "issue_time_utc": None,
        "valid_time_utc": None,
        "time_validation": "pending visual inspection of rendered official page",
        "official_source_url": url,
        "retrieval_time_utc": datetime.now(timezone.utc).isoformat(),
        "original_file": str(path.relative_to(OUT)),
        "normalized_pdf": str(path.relative_to(OUT)),
        "preview_files": previews,
        "mime_type": "application/pdf",
        "page_count": pages,
        "page_dimensions": dims,
        "byte_size": path.stat().st_size,
        "sha256": sha256(path),
        "headers": headers,
    })

list_url = "https://www.jma.go.jp/bosai/weather_map/data/list.json"
r = S.get(list_url, timeout=60)
print("GET", r.status_code, len(r.content), r.headers.get("content-type"), list_url, flush=True)
r.raise_for_status()
listing = r.json()
list_path = OUT / "weather_map_list.json"
list_path.write_text(json.dumps(listing, ensure_ascii=False, indent=2), encoding="utf-8")
strings = list(all_strings(listing))
print("LIST STRINGS", len(strings), flush=True)

surface_specs = [
    (3, "ASAS", "12", "asas"),
    (6, "ASAS", "18", "asas"),
    (9, "FSAS24", "12", "fsas24"),
]
for ordinal, product, cycle, listing_product in surface_specs:
    source_name = select_filename(strings, listing_product, cycle)
    if not source_name.endswith(".png"):
        source_name += ".png"
    png_url = f"https://www.jma.go.jp/bosai/weather_map/data/png/{source_name}"
    png_path = ORIG / f"{ordinal:02d}_{product}_{SOURCE_COMPACT}_{cycle}.png"
    headers = fetch(png_url, png_path, "image/png")
    pdf_path = ORIG / f"{ordinal:02d}_{product}_{SOURCE_COMPACT}_{cycle}.pdf"
    image_to_pdf(png_path, pdf_path)
    pages, dims = pdf_info(pdf_path)
    preview_path = PREV / f"{ordinal:02d}_{product}_{SOURCE_COMPACT}_{cycle}.png"
    preview_path.write_bytes(png_path.read_bytes())
    items.append({
        "ordinal": ordinal,
        "product_code": product,
        "cycle_utc": cycle,
        "source_date_utc": SOURCE_DATE,
        "issue_time_utc": source_name.split("_", 1)[0],
        "valid_time_utc": f"{SOURCE_DATE}T{cycle}:00:00Z" if product == "ASAS" else f"base {SOURCE_DATE}T{cycle}:00:00Z; +24 h",
        "time_validation": "validated from official source filename; printed time pending visual confirmation",
        "official_source_url": png_url,
        "retrieval_time_utc": datetime.now(timezone.utc).isoformat(),
        "original_file": str(png_path.relative_to(OUT)),
        "normalized_pdf": str(pdf_path.relative_to(OUT)),
        "preview_files": [str(preview_path.relative_to(OUT))],
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
    "list_url": list_url,
    "list_sha256": sha256(list_path),
    "items": items,
}
manifest_path = OUT / f"{RUN_DATE}_JMA_WeatherCharts_SourceManifest.json"
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
