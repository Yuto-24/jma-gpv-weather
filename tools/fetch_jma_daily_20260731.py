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
SOURCE_TS = "20260730120000"
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


def get(url: str) -> requests.Response:
    r = S.get(url, timeout=60)
    print("GET", r.status_code, len(r.content), r.headers.get("content-type"), url, flush=True)
    return r


def fetch(url: str, path: Path, required_prefix: str | None = None) -> dict[str, Any]:
    r = get(url)
    r.raise_for_status()
    ctype = r.headers.get("content-type") or ""
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


def fetch_first(urls: list[str], path: Path, prefixes: tuple[str, ...]) -> tuple[dict[str, Any], str]:
    attempts = []
    for url in urls:
        try:
            r = get(url)
            ctype = r.headers.get("content-type") or ""
            attempts.append({"url": url, "status": r.status_code, "content_type": ctype, "bytes": len(r.content)})
            if r.status_code == 200 and any(ctype.startswith(p) for p in prefixes):
                path.write_bytes(r.content)
                return ({
                    "url": url,
                    "content_type": ctype,
                    "last_modified": r.headers.get("last-modified"),
                    "etag": r.headers.get("etag"),
                    "date": r.headers.get("date"),
                    "attempts": attempts,
                }, ctype)
        except Exception as exc:
            attempts.append({"url": url, "error": repr(exc)})
    raise RuntimeError(f"No candidate succeeded: {json.dumps(attempts, ensure_ascii=False)}")


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


def normalize_to_pdf(src: Path, ctype: str, dest: Path) -> None:
    if ctype.startswith("application/pdf"):
        dest.write_bytes(src.read_bytes())
    elif ctype.startswith("image/"):
        image_to_pdf(src, dest)
    else:
        raise RuntimeError(f"Cannot normalize {ctype}")


def all_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from all_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from all_strings(v)


def select_surface_variants(strings: list[str], product: str, cycle: str) -> tuple[str, str]:
    target = f"_{SOURCE_COMPACT}{cycle}0000_MET_CHT_JCI{product.lower()}_"
    matches = sorted({s.split("/")[-1] for s in strings if target in s and (s.endswith(".png") or "_image" in s)})
    mono = [s for s in matches if "JRcolor" not in s]
    color = [s for s in matches if "JRcolor" in s]
    if len(mono) != 1 or len(color) != 1:
        raise RuntimeError(f"Expected one mono and one color {product} {cycle}: mono={mono}, color={color}")
    return mono[0], color[0]


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
        "standard_pdf": str(path.relative_to(OUT)),
        "preview_files": previews,
        "mime_type": "application/pdf",
        "page_count": pages,
        "page_dimensions": dims,
        "byte_size": path.stat().st_size,
        "sha256": sha256(path),
        "headers": headers,
    })

list_url = "https://www.jma.go.jp/bosai/weather_map/data/list.json"
r = get(list_url)
r.raise_for_status()
listing = r.json()
list_path = OUT / "weather_map_list.json"
list_path.write_text(json.dumps(listing, ensure_ascii=False, indent=2), encoding="utf-8")
strings = list(all_strings(listing))
print("LIST STRINGS", len(strings), flush=True)

# ASAS 12 and 18: exact entries remain in the live history list.
for ordinal, cycle in [(3, "12"), (6, "18")]:
    mono_name, color_name = select_surface_variants(strings, "asas", cycle)
    mono_url = f"https://www.jma.go.jp/bosai/weather_map/data/png/{mono_name}"
    color_url = f"https://www.jma.go.jp/bosai/weather_map/data/png/{color_name}"
    mono_png = ORIG / f"{ordinal:02d}_ASAS_{SOURCE_COMPACT}_{cycle}_mono.png"
    color_png = ORIG / f"{ordinal:02d}_ASAS_{SOURCE_COMPACT}_{cycle}_color.png"
    mono_headers = fetch(mono_url, mono_png, "image/png")
    color_headers = fetch(color_url, color_png, "image/png")
    mono_pdf = ORIG / f"{ordinal:02d}_ASAS_{SOURCE_COMPACT}_{cycle}_mono.pdf"
    color_pdf = ORIG / f"{ordinal:02d}_ASAS_{SOURCE_COMPACT}_{cycle}_color.pdf"
    image_to_pdf(mono_png, mono_pdf)
    image_to_pdf(color_png, color_pdf)
    pages, dims = pdf_info(mono_pdf)
    (PREV / mono_png.name).write_bytes(mono_png.read_bytes())
    (PREV / color_png.name).write_bytes(color_png.read_bytes())
    items.append({
        "ordinal": ordinal,
        "product_code": "ASAS",
        "cycle_utc": cycle,
        "source_date_utc": SOURCE_DATE,
        "issue_time_utc": mono_name.split("_", 1)[0],
        "valid_time_utc": f"{SOURCE_DATE}T{cycle}:00:00Z",
        "time_validation": "validated from official filenames; printed time pending visual confirmation",
        "official_source_url": mono_url,
        "official_color_source_url": color_url,
        "retrieval_time_utc": datetime.now(timezone.utc).isoformat(),
        "original_file": str(mono_png.relative_to(OUT)),
        "original_color_file": str(color_png.relative_to(OUT)),
        "standard_pdf": str(mono_pdf.relative_to(OUT)),
        "color_pdf": str(color_pdf.relative_to(OUT)),
        "preview_files": [str((PREV / mono_png.name).relative_to(OUT)), str((PREV / color_png.name).relative_to(OUT))],
        "mime_type": "image/png",
        "page_count": pages,
        "page_dimensions": dims,
        "byte_size": mono_png.stat().st_size,
        "sha256": sha256(mono_png),
        "color_byte_size": color_png.stat().st_size,
        "color_sha256": sha256(color_png),
        "standard_pdf_sha256": sha256(mono_pdf),
        "color_pdf_sha256": sha256(color_pdf),
        "source_filename": mono_name,
        "color_source_filename": color_name,
        "headers": mono_headers,
        "color_headers": color_headers,
    })

# FSAS24 12: use the monthly exact-cycle archive. Try documented host aliases and PDF/PNG forms.
month = SOURCE_TS[:6]
mono_candidates = [
    f"https://www.data.jma.go.jp/fcd/yoho/data/wxchart/quick/{month}/FSAS24_MONO_ASIA_{SOURCE_TS}.pdf",
    f"https://www.data.jma.go.jp/yoho/data/wxchart/quick/{month}/FSAS24_MONO_ASIA_{SOURCE_TS}.pdf",
    f"https://www.data.jma.go.jp/fcd/yoho/data/wxchart/quick/{month}/FSAS24_MONO_ASIA_{SOURCE_TS}.png",
    f"https://www.data.jma.go.jp/yoho/data/wxchart/quick/{month}/FSAS24_MONO_ASIA_{SOURCE_TS}.png",
    "https://www.jma.go.jp/jmh/wmapimgs/fsas24_12_large.png",
]
color_candidates = [
    f"https://www.data.jma.go.jp/fcd/yoho/data/wxchart/quick/{month}/FSAS24_COLOR_ASIA_{SOURCE_TS}.pdf",
    f"https://www.data.jma.go.jp/yoho/data/wxchart/quick/{month}/FSAS24_COLOR_ASIA_{SOURCE_TS}.pdf",
    f"https://www.data.jma.go.jp/fcd/yoho/data/wxchart/quick/{month}/FSAS24_COLOR_ASIA_{SOURCE_TS}.png",
    f"https://www.data.jma.go.jp/yoho/data/wxchart/quick/{month}/FSAS24_COLOR_ASIA_{SOURCE_TS}.png",
]
mono_raw = ORIG / f"09_FSAS24_{SOURCE_COMPACT}_12_mono.raw"
color_raw = ORIG / f"09_FSAS24_{SOURCE_COMPACT}_12_color.raw"
mono_headers, mono_type = fetch_first(mono_candidates, mono_raw, ("application/pdf", "image/"))
color_headers, color_type = fetch_first(color_candidates, color_raw, ("application/pdf", "image/"))
mono_ext = ".pdf" if mono_type.startswith("application/pdf") else ".png"
color_ext = ".pdf" if color_type.startswith("application/pdf") else ".png"
mono_source = mono_raw.with_suffix(mono_ext)
color_source = color_raw.with_suffix(color_ext)
mono_raw.rename(mono_source)
color_raw.rename(color_source)
mono_pdf = ORIG / f"09_FSAS24_{SOURCE_COMPACT}_12_mono.pdf"
color_pdf = ORIG / f"09_FSAS24_{SOURCE_COMPACT}_12_color.pdf"
normalize_to_pdf(mono_source, mono_type, mono_pdf)
normalize_to_pdf(color_source, color_type, color_pdf)
pages, dims = pdf_info(mono_pdf)
mono_previews = render_pdf(mono_pdf, f"09_FSAS24_{SOURCE_COMPACT}_12_mono")
color_previews = render_pdf(color_pdf, f"09_FSAS24_{SOURCE_COMPACT}_12_color")
items.append({
    "ordinal": 9,
    "product_code": "FSAS24",
    "cycle_utc": "12",
    "source_date_utc": SOURCE_DATE,
    "issue_time_utc": None,
    "valid_time_utc": "2026-07-31T12:00:00Z",
    "time_validation": "exact-cycle archive URL; printed time pending visual confirmation",
    "official_source_url": mono_headers["url"],
    "official_color_source_url": color_headers["url"],
    "retrieval_time_utc": datetime.now(timezone.utc).isoformat(),
    "original_file": str(mono_source.relative_to(OUT)),
    "original_color_file": str(color_source.relative_to(OUT)),
    "standard_pdf": str(mono_pdf.relative_to(OUT)),
    "color_pdf": str(color_pdf.relative_to(OUT)),
    "preview_files": mono_previews + color_previews,
    "mime_type": mono_type,
    "page_count": pages,
    "page_dimensions": dims,
    "byte_size": mono_source.stat().st_size,
    "sha256": sha256(mono_source),
    "color_byte_size": color_source.stat().st_size,
    "color_sha256": sha256(color_source),
    "standard_pdf_sha256": sha256(mono_pdf),
    "color_pdf_sha256": sha256(color_pdf),
    "headers": mono_headers,
    "color_headers": color_headers,
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
