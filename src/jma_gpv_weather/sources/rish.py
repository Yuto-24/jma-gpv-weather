"""RISH archive transport, independent of MSM run and filename rules."""
from datetime import date
from pathlib import Path
import time
import urllib.error
import urllib.request

from ..errors import GpvError
from ..models import RemoteFile

RISH_BASE = "http://database.rish.kyoto-u.ac.jp/arch/jmadata/data/gpv/original"

def _urlopen(url: str, timeout: int = 30, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, headers={
        "User-Agent": "jma-gpv-weather/0.3 (+educational-research)", **(headers or {})
    })
    return urllib.request.urlopen(request, timeout=timeout)

def read_listing(url: str, attempts: int = 3) -> str:
    last = None
    for attempt in range(attempts):
        try:
            with _urlopen(url.rstrip("/") + "/") as response:
                return response.read().decode("ascii", errors="ignore")
        except (OSError, urllib.error.URLError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise GpvError(f"RISH directory listing failed: {url}: {last}")

def download(remote: RemoteFile, destination: Path, attempts: int = 4) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    last = None
    for attempt in range(attempts):
        offset = partial.stat().st_size if partial.exists() else 0
        try:
            with _urlopen(remote.url, 60, {"Range": f"bytes={offset}-"} if offset else {}) as response:
                status = getattr(response, "status", 200)
                if offset and status != 206:
                    partial.unlink(missing_ok=True)
                    offset = 0
                with partial.open("ab" if offset and status == 206 else "wb") as output:
                    while chunk := response.read(1024*1024):
                        output.write(chunk)
            with partial.open("rb") as handle:
                if handle.read(4) != b"GRIB":
                    raise GpvError(f"Not a GRIB2 file: {remote.url}")
            partial.replace(destination)
            return destination
        except (OSError, urllib.error.URLError, GpvError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise GpvError(f"Download failed: {remote.url}: {last}")


class RishSource:
    def __init__(self, base_url: str = RISH_BASE):
        self.base_url = base_url

    def directory_url(self, day: date) -> str:
        return f"{self.base_url.rstrip('/')}/{day:%Y/%m/%d}"

    def read_listing(self, url: str) -> str:
        return read_listing(url)

    def download(self, remote: RemoteFile, destination: Path) -> Path:
        return download(remote, destination)
