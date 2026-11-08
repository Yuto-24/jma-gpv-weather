"""Opt-in desktop recovery using complete local MSM Runs after listing failure.

Call begin_calculation once per user action. No stale cache policy is applied to
MsmClient, portable consumers, or GSM. Forecast times are never shifted.
"""
from __future__ import annotations

import hashlib

from ..cache import cached_listing
from ..errors import GpvError, NoCompatibleRunError
from .client import MsmClient
from .spec import parse_listing, select_compatible_runs


class _ListingUnavailable(Exception):
    pass


class CachedMsmClient(MsmClient):
    """Desktop client with action-scoped, cache-only acquisition recovery."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.begin_calculation()

    def begin_calculation(self):
        self.using_cached_forecast = False
        self._listings: dict[str, str] = {}

    @property
    def acquisition_metadata(self):
        if not self.using_cached_forecast:
            return {}
        return {"acquisition": "cached", "reason": "LISTING_REFRESH_FAILED"}

    def _read_remote_listing(self, url):
        try:
            return self.source.read_listing(url)
        except (GpvError, OSError) as error:
            # Catch transport errors at the transport call, not parse/cache errors.
            raise _ListingUnavailable from error

    def _saved_listing(self, url):
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        path = self.cache_dir / "listings" / f"{key}.html"
        try:
            return path.read_text(encoding="ascii", errors="ignore")
        except FileNotFoundError:
            return ""

    def discover_runs(self, requirements, *, listings=None):
        if listings is not None:
            runs = super().discover_runs(requirements, listings=listings)
            return self._complete_cached_runs(runs) if self.using_cached_forecast else runs
        urls = self.listing_urls(requirements)
        if not self.using_cached_forecast:
            for url in urls:
                if url in self._listings:
                    continue
                try:
                    self._listings[url] = cached_listing(
                        url, self.cache_dir, self._read_remote_listing,
                    )
                except _ListingUnavailable:
                    self.using_cached_forecast = True
                    break
        if not self.using_cached_forecast:
            return super().discover_runs(requirements, listings=self._listings)

        files = []
        for url in urls:
            text = self._listings.get(url)
            if text is None:
                text = self._saved_listing(url)
                self._listings[url] = text
            files.extend(parse_listing(text, url))
        runs = select_compatible_runs(files, requirements)
        return self._complete_cached_runs(runs)

    def _complete_cached_runs(self, runs):
        runs = tuple(run for run in runs if all(
            (self.cache_dir / "raw" / f"{remote.run_utc:%Y%m%d%H%M%S}"
             / remote.name).is_file() for remote in run.files
        ))
        if not runs:
            raise NoCompatibleRunError("No complete cached MSM Run covers the requested times")
        return runs

    def prepare_run(self, run, requirements, available_runs=None,
                    terrain_provider=None, *, prepared_data=None):
        if prepared_data is not None:
            return super().prepare_run(run, requirements, available_runs,
                                       terrain_provider, prepared_data=prepared_data)
        runs = (self.discover_runs(requirements)
                if not available_runs or not self._listings else available_runs)
        return super().prepare_run(
            run, requirements, runs, terrain_provider,
            cache_only=self.using_cached_forecast,
        )
