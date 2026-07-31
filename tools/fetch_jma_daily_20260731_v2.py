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


def request(url: str, method: str = "GET") -> requests.Response:
    r = S.request(method, url, timeout=60, allow_redirects=True)
    print(method, r.status_code, len(r.content), r.headers.get("content-type"), url, flush=True)
    return r


def download(url: str, path: Path, prefixes: tuple[str, ...]) -> dict[str, Any]:
    r = request(url)
    ctype = (r.headers.get("content-type") or "").lower()
    r.raise_for_status()
    if not any(ctype.startswith(p) for p in prefixes):
        raise RuntimeError(f"Unexpected content type {ctype}: {url}")
    path.write_bytes(r.content)
    return {
        "url": url,
        "content_type": ctype,
        "last_modified": r.headers.get("last-modified"),
        "etag": r.headers.get("etag"),
        "date": r.headers.get("date"),
    }


def first_download(urls: list[str], path: Path, prefixes: tuple[str, ...]) -> tuple[dict[str, Any], str]:
    attempts: list[dict[str, Any]] = []
    for url in urls:
        try:
            r = request(url)
            ctype = (r.headers.get("content-type") or "").lower()
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
    raise RuntimeError(json.dumps(attempts, ensure_ascii=False))


def image_to_pdf(image: Path, pdf: Path) -> None:
    with Image.open(image) as im:
        if im.mode != "RGB":
            if "A" in im.getbands():
                bg = Image.new("RGB", im.size, "white")
                bg.paste(im, mask=im.getchannel("A"))
                im = bg
            else:
                im = im.convert("RGB")
        im.save(pdf, "PDF", resolution=200.0)


def normalize_pdf(src: Path, ctype: str, dest: Path) -> None:
    if ctype.startswith("application/pdf"):
        dest.write_bytes(src.read_bytes())
    elif ctype.startswith("image/"):
        image_to_pdf(src, dest)
    else:
        raise RuntimeError(ctype)


def pdf_info(path: Path) -> tuple[int, list[list[float]]]:
    with fitz.open(path) as doc:
        return doc.page_count, [[p.rect.width, p.rect.height] for p in doc]


def render(path: Path, stem: str) -> list[str]:
    outs: list[str] = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)
            out = PREV / f"{stem}_p{i+1}.png"
            pix.save(out)
            outs.append(str(out.relative_to(OUT)))
    return outs


def strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for x in value:
            yield from strings(x)
    elif isinstance(value, dict):
        for x in value.values():
            yield from strings(x)


def select_asas(all_names: list[str], cycle: str) -> tuple[str, str]:
    token = f"_{SOURCE_COMPACT}{cycle}0000_MET_CHT_JCIasas_"
    matches = sorted({x.split("/")[-1] for x in all_names if token in x and x.endswith(".png")})
    mono = [x for x in matches if "JRcolor" not in x]
    color = [x for x in matches if "JRcolor" in x]
    if len(mono) != 1 or len(color) != 1:
        raise RuntimeError(f"ASAS {cycle}: mono={mono}, color={color}")
    return mono[0], color[0]


def discover_fsas_names() -> tuple[str, str]:
    # Production normally occurs near 18:58 UTC. The embedded base time must remain exact.
    candidate_stamps = ["20260730185830", "20260730185831", "20260730185800"]
    for minute in range(50, 71):
        hour = 18 if minute < 60 else 19
        mm = minute if minute < 60 else minute - 60
        for ss in (0, 30, 31):
            stamp = f"20260730{hour:02d}{mm:02d}{ss:02d}"
            if stamp not in candidate_stamps:
                candidate_stamps.append(stamp)
    for stamp in candidate_stamps:
        color = f"{stamp}_0_Z__C_010000_{SOURCE_TS}_MET_CHT_JCIfsas24_JCP600x512_JRcolor_Tjmahp_image.png"
        url = f"https://www.jma.go.jp/bosai/weather_map/data/png/{color}"
        r = request(url, "HEAD")
        ctype = (r.headers.get("content-type") or "").lower()
        if r.status_code == 200 and ctype.startswith("image/"):
            mono = color.replace("JRcolor_Tjmahp", "Tjmahp")
            mono_url = f"https://www.jma.go.jp/bosai/weather_map/data/png/{mono}"
            mr = request(mono_url, "HEAD")
            if mr.status_code == 200 and (mr.headers.get("content-type") or "").lower().startswith("image/"):
                return mono, color
    raise RuntimeError("Exact FSAS24 weather_map filenames were not found in the bounded publication window")


items: list[dict[str, Any]] = []
for ordinal, product, filename in [
    (1, "AUPQ35", "aupq35_12.pdf"),
    (2, "AUPQ78", "aupq78_12.pdf"),
    (4, "FXJP854", "fxjp854_12.pdf"),
    (5, "AXFE578", "axfe578_12.pdf"),
    (7, "FXFE5782", "fxfe5782_12.pdf"),
    (8, "FXFE502", "fxfe502_12.pdf"),
]:
    url = f"https://www.jma.go.jp/bosai/numericmap/data/nwpmap/{filename}"
    path = ORIG / f"{ordinal:02d}_{product}_{SOURCE_COMPACT}_12.pdf"
    headers = download(url, path, ("application/pdf",))
    count, dims = pdf_info(path)
    if count != 1:
        raise RuntimeError(f"{product}: page count {count}")
    items.append({
        "ordinal": ordinal, "product_code": product, "cycle_utc": "12",
        "source_date_utc": SOURCE_DATE, "issue_time_utc": None, "valid_time_utc": None,
        "time_validation": "pending rendered-page inspection", "official_source_url": url,
        "retrieval_time_utc": datetime.now(timezone.utc).isoformat(),
        "original_file": str(path.relative_to(OUT)), "standard_pdf": str(path.relative_to(OUT)),
        "preview_files": render(path, f"{ordinal:02d}_{product}_{SOURCE_COMPACT}_12"),
        "mime_type": "application/pdf", "page_count": count, "page_dimensions": dims,
        "byte_size": path.stat().st_size, "sha256": sha256(path), "headers": headers,
    })

list_url = "https://www.jma.go.jp/bosai/weather_map/data/list.json"
listing_response = request(list_url)
listing_response.raise_for_status()
listing = listing_response.json()
list_path = OUT / "weather_map_list.json"
list_path.write_text(json.dumps(listing, ensure_ascii=False, indent=2), encoding="utf-8")
all_names = list(strings(listing))
for ordinal, cycle in ((3, "12"), (6, "18")):
    mono_name, color_name = select_asas(all_names, cycle)
    mono_url = f"https://www.jma.go.jp/bosai/weather_map/data/png/{mono_name}"
    color_url = f"https://www.jma.go.jp/bosai/weather_map/data/png/{color_name}"
    mono_png = ORIG / f"{ordinal:02d}_ASAS_{SOURCE_COMPACT}_{cycle}_mono.png"
    color_png = ORIG / f"{ordinal:02d}_ASAS_{SOURCE_COMPACT}_{cycle}_color.png"
    mh = download(mono_url, mono_png, ("image/",))
    ch = download(color_url, color_png, ("image/",))
    mono_pdf = ORIG / f"{ordinal:02d}_ASAS_{SOURCE_COMPACT}_{cycle}_mono.pdf"
    color_pdf = ORIG / f"{ordinal:02d}_ASAS_{SOURCE_COMPACT}_{cycle}_color.pdf"
    image_to_pdf(mono_png, mono_pdf)
    image_to_pdf(color_png, color_pdf)
    count, dims = pdf_info(mono_pdf)
    (PREV / mono_png.name).write_bytes(mono_png.read_bytes())
    (PREV / color_png.name).write_bytes(color_png.read_bytes())
    items.append({
        "ordinal": ordinal, "product_code": "ASAS", "cycle_utc": cycle,
        "source_date_utc": SOURCE_DATE, "issue_time_utc": mono_name.split("_", 1)[0],
        "valid_time_utc": f"{SOURCE_DATE}T{cycle}:00:00Z",
        "time_validation": "exact base time in official filename; pending printed-page inspection",
        "official_source_url": mono_url, "official_color_source_url": color_url,
        "retrieval_time_utc": datetime.now(timezone.utc).isoformat(),
        "original_file": str(mono_png.relative_to(OUT)), "original_color_file": str(color_png.relative_to(OUT)),
        "standard_pdf": str(mono_pdf.relative_to(OUT)), "color_pdf": str(color_pdf.relative_to(OUT)),
        "preview_files": [str((PREV / mono_png.name).relative_to(OUT)), str((PREV / color_png.name).relative_to(OUT))],
        "mime_type": "image/png", "page_count": count, "page_dimensions": dims,
        "byte_size": mono_png.stat().st_size, "sha256": sha256(mono_png),
        "color_byte_size": color_png.stat().st_size, "color_sha256": sha256(color_png),
        "standard_pdf_sha256": sha256(mono_pdf), "color_pdf_sha256": sha256(color_pdf),
        "source_filename": mono_name, "color_source_filename": color_name,
        "headers": mh, "color_headers": ch,
    })

# First seek an exact immutable filename. Static official endpoints are permitted only because their printed time is inspected later.
try:
    fs_mono_name, fs_color_name = discover_fsas_names()
    mono_candidates = [f"https://www.jma.go.jp/bosai/weather_map/data/png/{fs_mono_name}"]
    color_candidates = [f"https://www.jma.go.jp/bosai/weather_map/data/png/{fs_color_name}"]
    discovery = "exact base-time filename"
except Exception as discovery_error:
    print("FSAS exact-name discovery failed:", repr(discovery_error), flush=True)
    mono_candidates = [
        "https://www.jma.go.jp/jmh/wmapimgs/fsas24_12_large.png",
        "https://www.data.jma.go.jp/yoho/data/wxchart/quick/FSAS24_MONO_ASIA.pdf",
        "https://www.data.jma.go.jp/fcd/yoho/data/wxchart/quick/FSAS24_MONO_ASIA.pdf",
    ]
    color_candidates = [
        "https://www.data.jma.go.jp/yoho/data/wxchart/quick/FSAS24_COLOR_ASIA.pdf",
        "https://www.data.jma.go.jp/fcd/yoho/data/wxchart/quick/FSAS24_COLOR_ASIA.pdf",
    ]
    discovery = "cycle-specific/static official endpoint; exact printed time must be visually verified"

mono_raw = ORIG / "09_FSAS24_20260730_12_mono.raw"
color_raw = ORIG / "09_FSAS24_20260730_12_color.raw"
mh, mt = first_download(mono_candidates, mono_raw, ("application/pdf", "image/"))
ch, ct = first_download(color_candidates, color_raw, ("application/pdf", "image/"))
mono_source = mono_raw.with_suffix(".pdf" if mt.startswith("application/pdf") else ".png")
color_source = color_raw.with_suffix(".pdf" if ct.startswith("application/pdf") else ".png")
mono_raw.rename(mono_source)
color_raw.rename(color_source)
mono_pdf = ORIG / "09_FSAS24_20260730_12_mono.pdf"
color_pdf = ORIG / "09_FSAS24_20260730_12_color.pdf"
normalize_pdf(mono_source, mt, mono_pdf)
normalize_pdf(color_source, ct, color_pdf)
count, dims = pdf_info(mono_pdf)
items.append({
    "ordinal": 9, "product_code": "FSAS24", "cycle_utc": "12",
    "source_date_utc": SOURCE_DATE, "issue_time_utc": None,
    "valid_time_utc": "2026-07-31T12:00:00Z", "time_validation": discovery,
    "official_source_url": mh["url"], "official_color_source_url": ch["url"],
    "retrieval_time_utc": datetime.now(timezone.utc).isoformat(),
    "original_file": str(mono_source.relative_to(OUT)), "original_color_file": str(color_source.relative_to(OUT)),
    "standard_pdf": str(mono_pdf.relative_to(OUT)), "color_pdf": str(color_pdf.relative_to(OUT)),
    "preview_files": render(mono_pdf, "09_FSAS24_20260730_12_mono") + render(color_pdf, "09_FSAS24_20260730_12_color"),
    "mime_type": mt, "page_count": count, "page_dimensions": dims,
    "byte_size": mono_source.stat().st_size, "sha256": sha256(mono_source),
    "color_byte_size": color_source.stat().st_size, "color_sha256": sha256(color_source),
    "standard_pdf_sha256": sha256(mono_pdf), "color_pdf_sha256": sha256(color_pdf),
    "headers": mh, "color_headers": ch,
})

items.sort(key=lambda x: x["ordinal"])
manifest = {
    "run_date_jst": RUN_DATE, "source_date_utc": SOURCE_DATE,
    "retrieval_time_utc": datetime.now(timezone.utc).isoformat(),
    "list_url": list_url, "list_sha256": sha256(list_path), "items": items,
}
(OUT / f"{RUN_DATE}_JMA_WeatherCharts_SourceManifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
