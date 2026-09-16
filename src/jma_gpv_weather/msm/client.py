from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping, TYPE_CHECKING

from ..cache import cached_listing
from ..errors import MissingVariableError, NoCompatibleRunError, SelectedRunCoverageError
from ..models import (
    Bounds, RemoteFile, RunSelection, ForecastRequirements,
    ForecastRunStatus, RunId, WeatherVariable,
)
from ..sources import DataSource
from ..sources.rish import RISH_BASE, RishSource
from ..time_utils import UTC
from ._errors import msm_error_boundary
from .spec import (
    DEFAULT_BOUNDS, LEVELS_HPA, MAX_FORECAST_HOURS, interpolation_bounds,
    parse_listing, required_valid_times, select_compatible_runs, check_coverage,
)

if TYPE_CHECKING:
    from .prepared import MsmPreparedData

class MsmClient:
    check_coverage = staticmethod(check_coverage)

    def __init__(
        self,
        cache_dir: str | Path = "data",
        bounds: Bounds = DEFAULT_BOUNDS,
        base_url: str = RISH_BASE,
        *,
        source: DataSource | None = None,
    ):
        self.cache_dir = Path(cache_dir)
        self.bounds = bounds
        self.base_url = base_url
        self.source = source if source is not None else RishSource(base_url)

    def listing_urls(self, requirements: ForecastRequirements) -> tuple[str, ...]:
        """Directories to acquire through DataSource or an async runtime transport."""
        needed = required_valid_times(requirements)
        earliest = min(time for values in needed.values() for time in values)
        latest = max(time for values in needed.values() for time in values)
        first_day = (earliest - timedelta(hours=MAX_FORECAST_HOURS)).date()
        last_day = min(latest, datetime.now(UTC)).date()
        urls = []
        day = first_day
        while day <= last_day:
            urls.append(self.source.directory_url(day))
            day += timedelta(days=1)
        return tuple(urls)

    def discover_runs(
        self, requirements: ForecastRequirements, *, listings: Mapping[str, str] | None = None,
    ) -> tuple[RunSelection, ...]:
        """Select runs from desktop transport or a complete URL -> listing mapping.

        Supplied listings bypass filesystem caching. Missing entries are acquisition
        failures, not evidence of absent runs; use an empty string for a known empty
        directory. Runtime transports must propagate their own acquisition errors.
        """
        from ..errors import MsmError
        files: list[RemoteFile] = []
        for directory in self.listing_urls(requirements):
            if listings is not None:
                if directory not in listings or not isinstance(listings[directory], str):
                    raise MsmError(f"Missing or invalid acquired listing: {directory}")
                try:
                    files.extend(parse_listing(listings[directory], directory))
                except ValueError as exc:
                    raise MsmError(f"Invalid acquired listing: {directory}: {exc}") from exc
                continue
            try:
                listing = cached_listing(directory, self.cache_dir, self.source.read_listing)
                files.extend(parse_listing(listing, directory))
            except Exception:
                pass
        runs = select_compatible_runs(files, requirements)
        if not runs:
            raise NoCompatibleRunError("No forecast run covers all required interpolation times")
        return runs

    def resolve_run(
        self,
        requirements: ForecastRequirements,
        selected_run: RunId | None = None,
        available_runs: tuple[RunSelection, ...] | None = None,
    ) -> ForecastRunStatus:
        runs = available_runs or self.discover_runs(requirements)
        latest = RunId(runs[0].run_utc)
        run_ids = {RunId(run.run_utc) for run in runs}
        if selected_run is None:
            return ForecastRunStatus(latest, latest, False, True)
        covers = selected_run in run_ids
        update = covers and latest.initial_time_utc > selected_run.initial_time_utc
        warnings = ("UPDATE_AVAILABLE",) if update else ()
        if not covers:
            warnings = ("SELECTED_RUN_OUT_OF_COVERAGE",)
        return ForecastRunStatus(selected_run, latest, update, covers, warnings)

    def prepare_run(
        self,
        run: RunId,
        requirements: ForecastRequirements,
        available_runs: tuple[RunSelection, ...] | None = None,
        terrain_provider=None,
        *,
        prepared_data: MsmPreparedData | None = None,
    ):
        """Prepare through the desktop cache or validate supplied portable MSM data.

        Supplied data performs no acquisition, decode or filesystem cache access.
        Its record grids define the prepared area; bounds controls desktop decode.
        Without available_runs, compatibility is checked against its source files.
        """
        from ..cache import acquire_files
        from .dataset import PreparedForecast
        from ..normalized import prepare_records

        if prepared_data is not None:
            from .prepared import MsmPreparedData
            if not isinstance(prepared_data, MsmPreparedData):
                raise TypeError("prepared_data must be MsmPreparedData")
            prepared_data.validate()
            if available_runs is None:
                available_runs = select_compatible_runs(prepared_data.selection.files, requirements)
            runs = available_runs
        else:
            runs = available_runs or self.discover_runs(requirements)
        selection = next(
            (candidate for candidate in runs if candidate.run_utc == run.initial_time_utc),
            None,
        )
        if selection is None:
            raise SelectedRunCoverageError(
                f"selected run {run} does not cover all required interpolation times"
            )
        if prepared_data is not None:
            return prepared_data._prepare(selection, requirements, terrain_provider=terrain_provider)
        with msm_error_boundary():
            paths, hashes = acquire_files(selection.files, self.cache_dir, self.source.download)
        needed = required_valid_times(requirements)
        valid_times = tuple(
            sorted({value for values in needed.values() for value in values})
        )
        prepared_bounds = interpolation_bounds(self.bounds)
        with msm_error_boundary():
            surface, pressure = prepare_records(
                self.cache_dir, run, prepared_bounds, valid_times, paths, hashes,
                pressure_levels=LEVELS_HPA,
            )
        self._validate_prepared(requirements, needed, surface, pressure)
        return PreparedForecast(
            selection,
            surface,
            pressure,
            hashes,
            terrain_provider=terrain_provider,
        )

    @staticmethod
    def _validate_prepared(requirements, needed, surface, pressure):
        missing = []
        if WeatherVariable.SURFACE_WIND in requirements.variables:
            for valid in needed.get("Lsurf", ()):
                for name in ("u", "v"):
                    if not any(key[0] == valid and key[2] == name for key in surface):
                        missing.append(f"{valid.isoformat()}:surface:{name}")
        if WeatherVariable.SURFACE_TEMPERATURE in requirements.variables:
            for valid in needed.get("Lsurf", ()):
                if not any(key[0] == valid and key[2] == "tmp_surface" for key in surface):
                    missing.append(f"{valid.isoformat()}:surface:tmp_surface")
        if WeatherVariable.ESTIMATED_QNH in requirements.variables:
            for valid in needed.get("Lsurf", ()):
                for name in ("sp", "tmp_surface", "rh"):
                    if not any(key[0] == valid and key[2] == name for key in surface):
                        missing.append(f"{valid.isoformat()}:surface:{name}")
        pressure_names = {"hgt"}
        if WeatherVariable.ALOFT_WIND in requirements.variables:
            pressure_names.update(("u", "v"))
        if WeatherVariable.ALOFT_TEMPERATURE in requirements.variables:
            pressure_names.add("tmp")
        if pressure_names != {"hgt"}:
            for valid in needed.get("L-pall", ()):
                for level in LEVELS_HPA:
                    for name in pressure_names:
                        if (valid, level, name) not in pressure:
                            missing.append(f"{valid.isoformat()}:{level}hPa:{name}")
        if missing:
            raise MissingVariableError(
                "required MSM fields are missing: " + ", ".join(missing[:12])
            )
