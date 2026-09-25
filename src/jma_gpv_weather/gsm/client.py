"""Explicit GSM Japan client. No model/source fallback policy."""
from datetime import datetime
from pathlib import Path

from ..cache import acquire_files, cached_listing, file_lock
from ..coverage import CoveragePoint
from ..errors import (GsmCoverageError, GsmDiscoveryError, GsmRunUnavailableError,
                      GsmProcessingError)
from ..models import Bounds, ForecastRunStatus, RunId, WeatherVariable
from ..normalized import prepare_records
from ..sources.rish import RISH_BASE, RishSource
from ..time_utils import UTC
from . import spec


class GsmClient:
    def __init__(self, cache_dir="data", bounds=Bounds(), base_url=RISH_BASE, *, source=None):
        # Every cache artifact (including listings/manifests/locks) is rooted here.
        self.cache_dir = Path(cache_dir) / "gsm-japan"
        self.bounds = bounds
        self.source = RishSource(base_url) if source is None else source
        if not (bounds.lat_min <= bounds.lat_max and bounds.lon_min <= bounds.lon_max):
            raise ValueError("bounds must be ordered")
        # Reject non-finite bounds before any source access.
        CoveragePoint(bounds.lat_min, bounds.lon_min)
        CoveragePoint(bounds.lat_max, bounds.lon_max)

    check_coverage = staticmethod(spec.check_coverage)

    def _coverage(self, requirements, run=None, as_of=None):
        result = self.check_coverage(
            requirements, run=run, as_of=as_of,
            points=(CoveragePoint(self.bounds.lat_min, self.bounds.lon_min),
                    CoveragePoint(self.bounds.lat_max, self.bounds.lon_max)),
        )
        if result.outside_spec:
            raise GsmCoverageError(result)
        return result

    def listing_urls(self, requirements, *, as_of=None):
        """Required source directories; no acquisition or filesystem access."""
        coverage = self._coverage(requirements, as_of=as_of)
        days = sorted({r.initial_time_utc.date() for r in coverage.candidate_runs}, reverse=True)
        return tuple(self.source.directory_url(day) for day in days)

    def discover_runs(self, requirements, *, as_of=None, listings=None):
        """Discover from desktop cache or a complete acquired URL -> text mapping.

        Missing entries are discovery failures, never an observed empty directory.
        Supplied listings bypass acquisition and filesystem cache entirely.
        """
        coverage = self._coverage(requirements, as_of=as_of)
        files = []
        for directory in self.listing_urls(requirements, as_of=as_of):
            try:
                if listings is None:
                    listing = cached_listing(directory, self.cache_dir, self.source.read_listing)
                else:
                    if directory not in listings or not isinstance(listings[directory], str):
                        raise ValueError(f"Missing or invalid acquired listing: {directory}")
                    listing = listings[directory]
                files.extend(spec.parse_listing(listing, directory))
            except Exception as exc:
                # Fail explicitly, even when an older day succeeded: claiming the
                # latest compatible run would otherwise hide a partial discovery.
                raise GsmDiscoveryError(f"GSM listing failed: {directory}: {exc}") from exc
        candidates = {r.initial_time_utc for r in coverage.candidate_runs}
        runs = spec.select_compatible_runs((f for f in files if f.run_utc in candidates), requirements)
        if not runs:
            raise GsmRunUnavailableError("No RISH GSM run/files found for a specification-compatible request")
        return runs

    def _runs(self, requirements, available_runs):
        self._coverage(requirements)
        if available_runs is None:
            return self.discover_runs(requirements)
        # Never trust an injected selection that combines different runs.
        files = [f for r in available_runs for f in r.files if f.run_utc == r.run_utc]
        now = datetime.now(UTC)
        return tuple(r for r in spec.select_compatible_runs(files, requirements) if r.run_utc <= now)

    def resolve_run(self, requirements, selected_run=None, available_runs=None):
        try:
            runs = self._runs(requirements, available_runs)
        except GsmRunUnavailableError:
            if selected_run is None:
                raise
            runs = ()
        latest = RunId(runs[0].run_utc) if runs else None
        if selected_run is None:
            if latest is None:
                raise GsmRunUnavailableError("No compatible GSM run/files discovered")
            return ForecastRunStatus(latest, latest, False, True)
        spec_covers = not self.check_coverage(requirements, run=selected_run).outside_spec
        found = any(r.run_utc == selected_run.initial_time_utc for r in runs)
        update = spec_covers and found and latest is not None and latest.initial_time_utc > selected_run.initial_time_utc
        warnings = (("SELECTED_RUN_OUTSIDE_SPEC",) if not spec_covers else
                    ("SELECTED_RUN_NOT_DISCOVERED",) if not found else
                    ("UPDATE_AVAILABLE",) if update else ())
        return ForecastRunStatus(selected_run, latest, update, spec_covers and found, warnings)

    def prepare_run(self, run, requirements, available_runs=None, *, prepared_data=None):
        """Prepare desktop data or validate portable data without I/O/native decode."""
        from .dataset import PreparedGsmForecast
        run = RunId(run.initial_time_utc.astimezone(UTC))
        self._coverage(requirements, run=run)
        if prepared_data is not None:
            from .prepared import GsmPreparedData
            if not isinstance(prepared_data, GsmPreparedData):
                raise TypeError("prepared_data must be GsmPreparedData")
            prepared_data.validate()
            if available_runs is None:
                available_runs = spec.select_compatible_runs(prepared_data.selection.files, requirements)
        runs = self._runs(requirements, available_runs)
        selection = next((r for r in runs if r.run_utc == run.initial_time_utc), None)
        if selection is None:
            raise GsmRunUnavailableError(f"Selected GSM run {run} was not discovered with all required files")
        if prepared_data is not None:
            return prepared_data._prepare(selection, requirements)
        needed = spec.required_valid_times(requirements, run.initial_time_utc)
        times = tuple(sorted({t for ts in needed.values() for t in ts}))

        def validate_records(surface, pressure):
            # The common cache layer only needs a model-neutral invalid-record
            # signal; preserve GSM's detailed diagnostic in the cause chain.
            try:
                self._validate_prepared(requirements, needed, surface, pressure)
            except GsmProcessingError as exc:
                raise ValueError(str(exc)) from exc

        try:
            # Serialize run-level raw manifest writes, including concurrent queries
            # for disjoint file sets. Common raw locks still protect each file.
            with file_lock(self.cache_dir / "locks" / f"prepare-{run}.lock"):
                paths, hashes = acquire_files(selection.files, self.cache_dir, self.source.download)
                surface, pressure = prepare_records(
                    self.cache_dir, run, spec.interpolation_bounds(self.bounds), times, paths, hashes,
                    pressure_levels=spec.LEVELS_HPA, verify_manifest=True,
                    validate_records=validate_records, surface_temperature_level=2,
                )
        except GsmProcessingError:
            raise
        except Exception as exc:
            raise GsmProcessingError(f"GSM acquisition/processing failed for {run}: {exc}") from exc
        return PreparedGsmForecast(selection, surface, pressure, hashes, requirements)

    @staticmethod
    def _validate_prepared(requirements, needed, surface, pressure):
        missing = []
        for valid in needed.get('Lsurf', ()):
            if (valid, 2, 'tmp_surface') not in surface:
                missing.append(f'{valid}:2m:tmp_surface')
        names = {'hgt'}
        if WeatherVariable.ALOFT_WIND in requirements.variables:
            names.update(('u', 'v'))
        if WeatherVariable.ALOFT_TEMPERATURE in requirements.variables:
            names.add('tmp')
        for valid in needed.get('L-pall', ()):
            for level in spec.LEVELS_HPA:
                for name in names:
                    if (valid, level, name) not in pressure:
                        missing.append(f'{valid}:{level}:{name}')
        if missing:
            raise GsmProcessingError('Required GSM fields missing: ' + ', '.join(missing[:12]))
