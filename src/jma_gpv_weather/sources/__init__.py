"""Transport boundary; forecast-model parsing belongs to the model package."""
from datetime import date
from pathlib import Path
from typing import Protocol

from ..models import RemoteFile


class DataSource(Protocol):
    """Archive transport; download creates parents and atomically replaces its target."""

    def directory_url(self, day: date) -> str: ...
    def read_listing(self, url: str) -> str: ...
    def download(self, remote: RemoteFile, destination: Path) -> Path: ...
