from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz
import requests

BASE = "20260730120000"
OUT = Path("fsas24-20260730")
OUT.mkdir(parents=True, exist_ok=True)
ROOT = "https://www.jma.go.jp/bosai/weather_map/data/png/"


def candidate(stamp: str, color: bool) -> str:
    suffix = "JRcolor_Tjmahp" if color else "JRjmahp"
    return f"{stamp}_0_Z__C_010000_{BASE}_MET_CHT_JCIfsas24_JCP600x512_{suffix}_image.png"


def probe(stamp: str) -> tuple[str, int, str]:
    name = candidate(stamp, True)
    r = requests.head(ROOT + name, timeout=15, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
    return stamp, r.status_code, (r.headers.get("content-type") or "").lower()


priority = ["20260730192530", "20260730192630", "20260730192430", "20260730192531", "20260730192631"]
stamps = priority + [f"2026073019{minute:02d}{second:02d}" for minute in range(15, 36) for second in range(60)]
stamps = list(dict.fromkeys(stamps))
found = None
with ThreadPoolExecutor(max_workers=24) as ex:
    futures = {ex.submit(probe, stamp): stamp for stamp in stamps}
    for fut in as_completed(futures):
        stamp, status, ctype = fut.result()
        if status == 200 and ctype.startswith("image/"):
            found = stamp
            break
if found is None:
    raise RuntimeError("No exact FSAS24 color image found for base 20260730120000 in 19:15–19:35 UTC issue window")

results = {}
for variant, color in (("mono", False), ("color", True)):
    name = candidate(found, color)
    url = ROOT + name
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    ctype = (r.headers.get("content-type") or "").lower()
    print("GET", r.status_code, ctype, len(r.content), url, flush=True)
    r.raise_for_status()
    if not ctype.startswith("image/"):
        raise RuntimeError(f"Unexpected content type: {ctype}")
    path = OUT / f"09_FSAS24_20260730_12_{variant}.png"
    path.write_bytes(r.content)
    h = hashlib.sha256(r.content).hexdigest()
    doc = fitz.open()
    img = fitz.open(path)
    pdfbytes = img.convert_to_pdf()
    img.close()
    pdf = fitz.open("pdf", pdfbytes)
    outpdf = OUT / f"09_FSAS24_20260730_12_{variant}.pdf"
    pdf.save(outpdf)
    page = pdf[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(3,3), alpha=False)
    preview = OUT / f"09_FSAS24_20260730_12_{variant}_preview.png"
    pix.save(preview)
    pdf.close(); doc.close()
    results[variant] = {
        "issue_timestamp": found,
        "base_timestamp": BASE,
        "source_filename": name,
        "source_url": url,
        "last_modified": r.headers.get("last-modified"),
        "etag": r.headers.get("etag"),
        "bytes": path.stat().st_size,
        "sha256": h,
        "pdf_sha256": hashlib.sha256(outpdf.read_bytes()).hexdigest(),
    }

(OUT / "fsas24_manifest.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
