"""Portable GSM Japan records, independent of GRIB, NetCDF, transport and file locks."""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime
import re

import numpy as np

from ..errors import GsmCacheIntegrityError
from ..models import RemoteFile, RunSelection
from ..prepared import encode_prepared, decode_prepared
from ..weather import RecordMap
from .spec import DOMAIN, LEVELS_HPA, forecast_hours, parse_listing, required_valid_times, select_compatible_runs


@dataclass
class GsmPreparedData:
    """Decoded SI-unit records with original GRIB source identity and hashes.

    Keys are (aware valid time, level, variable); values are (values, latitude,
    longitude), all matching 2-D arrays. Pressure levels are hPa, HGT is metres,
    temperature kelvin and wind m/s. Surface levels follow the desktop decoder.
    NaN source values remain missing data, never inferred coverage exclusions.
    """

    selection: RunSelection
    surface: RecordMap
    pressure: RecordMap
    source_hashes: dict[str, str]

    @classmethod
    def from_forecast(cls, forecast):
        """Snapshot a desktop PreparedGsmForecast without exposing private records."""
        from .dataset import PreparedGsmForecast
        if not isinstance(forecast, PreparedGsmForecast):
            raise TypeError("forecast must be a GSM PreparedGsmForecast")
        surface, pressure = deepcopy((dict(forecast.surface), dict(forecast.pressure)))
        result = cls(forecast.selection, surface, pressure,
                     dict(forecast.source_hashes))
        result.validate()
        return result

    def validate(self):
        """Reject malformed/foreign data; preserve missing-value query semantics."""
        try:
            self._validate()
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            raise GsmCacheIntegrityError(f"Invalid GSM prepared data: {exc}") from exc

    def _validate(self):
        def invalid(message):
            raise GsmCacheIntegrityError(f"Invalid GSM prepared data: {message}")

        if not isinstance(self.selection, RunSelection) or not self.selection.files:
            invalid("missing source selection")
        urls = set()
        for remote in self.selection.files:
            if not isinstance(remote, RemoteFile) or not isinstance(remote.url, str):
                invalid("invalid source file")
            parsed = parse_listing(remote.name, remote.url.rsplit('/', 1)[0])
            if (parsed != [remote] or remote.run_utc != self.selection.run_utc
                    or remote.url in urls):
                invalid("source/model/run identity mismatch")
            urls.add(remote.url)
        if set(self.source_hashes) != urls or any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in self.source_hashes.values()
        ):
            invalid("source SHA-256 mapping mismatch")
        # Preserve the common decoder's ancillary surface fields. GSM's public
        # query contract still exposes only the 2 m temperature from this group.
        surface_levels = {"u": (10,), "v": (10,), "tmp_surface": (0, 2),
                          "rh": (0, 2), "sp": (0,), "mslp": (0,)}
        for kind, records in (("Lsurf", self.surface), ("L-pall", self.pressure)):
            reference = None
            identities = set()
            for key, record in records.items():
                if not isinstance(key, tuple) or len(key) != 3:
                    invalid("invalid record key")
                valid, level, name = key
                if (not isinstance(valid, datetime) or valid.tzinfo is None
                        or not isinstance(level, int) or isinstance(level, bool)):
                    invalid("invalid record time/level")
                allowed = (level in surface_levels.get(name, ()) if kind == "Lsurf"
                           else level in LEVELS_HPA and name in ("hgt", "u", "v", "tmp"))
                hour = (valid - self.selection.run_utc).total_seconds() / 3600
                if (not allowed or hour not in forecast_hours(self.selection.run_utc, kind)
                        or not any(f.kind == kind and f.first_hour <= hour <= f.last_hour
                                   for f in self.selection.files)):
                    invalid("record outside source product/time/level")
                identity = (valid, name, level if kind == "L-pall" else None)
                if identity in identities:
                    invalid("ambiguous surface level")
                identities.add(identity)
                if not isinstance(record, (tuple, list)) or len(record) != 3:
                    invalid("invalid record arrays")
                if any(not isinstance(a, np.ndarray) or np.ma.isMaskedArray(a) for a in record):
                    invalid("records require NumPy arrays; represent missing values with NaN")
                values, lat, lon = record
                if (values.ndim != 2 or not values.size or lat.shape != values.shape
                        or lon.shape != values.shape or any(a.dtype.kind not in "fiu" for a in (values, lat, lon))):
                    invalid("invalid array shape/dtype")
                if (not np.isfinite(lat).all() or not np.isfinite(lon).all()
                        or not np.array_equal(lat, np.broadcast_to(lat[:, :1], lat.shape))
                        or not np.array_equal(lon, np.broadcast_to(lon[:1, :], lon.shape))
                        or np.any((lat < DOMAIN.lat_min) | (lat > DOMAIN.lat_max))
                        or np.any((lon < DOMAIN.lon_min) | (lon > DOMAIN.lon_max))):
                    invalid("invalid GSM grid")
                for axis in (lat[:, 0], lon[0, :]):
                    delta = np.diff(axis.astype(float, copy=False))
                    if not ((delta > 0).all() or (delta < 0).all()):
                        invalid("non-monotonic grid")
                if reference is not None and any(
                    not np.array_equal(a, b) for a, b in zip(reference, (lat, lon))
                ):
                    invalid("inconsistent product grids")
                reference = lat, lon

    def _prepare(self, selection, requirements):
        """Internal assembly used by GsmClient after its ordinary Run selection."""
        from .client import GsmClient
        from .dataset import PreparedGsmForecast
        if (selection.run_utc != self.selection.run_utc
                or any(f not in self.selection.files for f in selection.files)):
            raise GsmCacheIntegrityError("GSM prepared data does not match selected source files")
        if not select_compatible_runs(selection.files, requirements):
            raise GsmCacheIntegrityError("Selected source files do not cover the request")

        def selected_records(records, kind):
            files = tuple(remote for remote in selection.files if remote.kind == kind)
            return {key: record for key, record in records.items()
                    if any(remote.first_hour <= (key[0] - remote.run_utc).total_seconds() / 3600
                           <= remote.last_hour for remote in files)}

        surface = selected_records(self.surface, "Lsurf")
        pressure = selected_records(self.pressure, "L-pall")
        GsmClient._validate_prepared(requirements, required_valid_times(requirements, selection.run_utc),
                                     surface, pressure)
        source_hashes = {remote.url: self.source_hashes[remote.url] for remote in selection.files}
        return PreparedGsmForecast(selection, surface, pressure,
                                source_hashes, requirements)

    def to_bytes(self) -> bytes:
        """Versioned NPZ snapshot with bounded, pickle-free public decoding."""
        return encode_prepared(self, "GSM_JAPAN")

    @classmethod
    def from_bytes(cls, payload: bytes, *, expected_sha256: str | None = None):
        """Validate model, hash and bounded ZIP/NPY allocation before loading.

        Limits: 32 MiB compressed, 2048 members, 32 MiB/member, 128 MiB expanded,
        1 MiB metadata. Source hashes describe GRIB inputs, not this payload.
        """
        return decode_prepared(cls, payload, model="GSM_JAPAN", error_type=GsmCacheIntegrityError,
                               expected_sha256=expected_sha256)
