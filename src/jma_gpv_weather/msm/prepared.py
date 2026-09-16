"""Portable MSM records, independent of GRIB, NetCDF, transport and file locks."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import deepcopy
from datetime import datetime
import hashlib
from io import BytesIO
import json
import math
import re
import struct
from zipfile import BadZipFile, ZipFile, ZIP_STORED, ZIP_DEFLATED
import zlib

import numpy as np

from ..errors import CacheIntegrityError, SelectedRunCoverageError
from ..models import RemoteFile, RunSelection
from ..weather import RecordMap
from .spec import LEVELS_HPA, parse_listing, required_valid_times, select_compatible_runs

# Portable input budgets, independent of the desktop raw/NetCDF cache. Bound both
# compressed input and advertised allocations before NumPy reads any arrays.
_MAX_PAYLOAD_BYTES = 32 * 1024**2
_MAX_MEMBERS = 2048
_MAX_MEMBER_BYTES = 32 * 1024**2
_MAX_TOTAL_BYTES = 128 * 1024**2
_MAX_METADATA_BYTES = 1024**2
_MAX_NPY_HEADER_BYTES = 10000


def _validate_archive(payload):
    """Preflight ZIP sizes and NPY allocation headers without loading arrays."""
    # Bound directory parsing before ZipFile allocates one ZipInfo per entry.
    # Our bounded snapshots need neither multidisk nor ZIP64 central directories
    # (NumPy's ZIP64 local member headers are still supported).
    end = payload.rfind(b"PK\x05\x06", max(0, len(payload) - 65557))
    if end < 0 or end + 22 > len(payload):
        raise CacheIntegrityError("Invalid MSM prepared ZIP directory")
    _, disk, start_disk, disk_count, count, size, offset, comment_size = struct.unpack_from(
        "<4s4H2IH", payload, end
    )
    if count > _MAX_MEMBERS:
        raise CacheIntegrityError("MSM prepared archive exceeds member count limit")
    if (disk or start_disk or disk_count != count or offset + size != end
            or end + 22 + comment_size != len(payload)):
        raise CacheIntegrityError("Unsupported MSM prepared ZIP directory")
    position, actual_count = offset, 0
    while position < end:
        if position + 46 > end or payload[position:position + 4] != b"PK\x01\x02":
            raise CacheIntegrityError("Invalid MSM prepared ZIP directory entry")
        name_size, extra_size, entry_comment_size = struct.unpack_from("<HHH", payload, position + 28)
        actual_count += 1
        if actual_count > _MAX_MEMBERS:
            raise CacheIntegrityError("MSM prepared archive exceeds member count limit")
        if name_size > 64:
            raise CacheIntegrityError("Invalid MSM prepared ZIP member name size")
        position += 46 + name_size + extra_size + entry_comment_size
    if position != end or actual_count != count:
        raise CacheIntegrityError("Inconsistent MSM prepared ZIP member count")
    with ZipFile(BytesIO(payload)) as archive:
        members = archive.infolist()
        if len(members) > _MAX_MEMBERS:
            raise CacheIntegrityError("MSM prepared archive exceeds member count limit")
        if any(member.file_size > _MAX_MEMBER_BYTES for member in members):
            raise CacheIntegrityError("MSM prepared archive exceeds member size limit")
        if sum(member.file_size for member in members) > _MAX_TOTAL_BYTES:
            raise CacheIntegrityError("MSM prepared archive exceeds total expanded size limit")
        names = [member.filename for member in members]
        if any(re.fullmatch(r"(metadata|(surface|pressure)_(latitude|longitude)|r[0-9]+)\.npy", name)
               is None for name in names):
            raise CacheIntegrityError("MSM prepared archive has noncanonical member names")
        if len(set(names)) != len(names) or "metadata.npy" not in names:
            raise CacheIntegrityError("MSM prepared archive has duplicate or missing members")
        if archive.getinfo("metadata.npy").file_size > _MAX_METADATA_BYTES:
            raise CacheIntegrityError("MSM prepared archive exceeds metadata size limit")
        if any(member.compress_type not in (ZIP_STORED, ZIP_DEFLATED) for member in members):
            raise CacheIntegrityError("Unsupported MSM prepared compression method")
        for member in members:
            with archive.open(member) as stream:
                # NumPy checks header length after reading it. A bounded buffer
                # prevents a forged length from requesting unlimited inflation.
                header = BytesIO(stream.read(_MAX_NPY_HEADER_BYTES + 12))
                version = np.lib.format.read_magic(header)
                readers = {(1, 0): np.lib.format.read_array_header_1_0,
                           (2, 0): np.lib.format.read_array_header_2_0}
                if version not in readers:
                    raise CacheIntegrityError("Unsupported MSM prepared NPY version")
                shape, _, dtype = readers[version](header, max_header_size=_MAX_NPY_HEADER_BYTES)
                # A tiny NPY file can claim an enormous shape even when all ZIP
                # sizes pass. Match its allocation to the bounded member size.
                if (dtype.kind not in "fiu" or any(size < 0 for size in shape)
                        or math.prod(shape) * dtype.itemsize != member.file_size - header.tell()):
                    raise CacheIntegrityError("MSM prepared array size/dtype does not match ZIP member")


@dataclass
class MsmPreparedData:
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
        """Snapshot a desktop PreparedForecast without exposing private records."""
        from .dataset import PreparedForecast
        if not isinstance(forecast, PreparedForecast):
            raise TypeError("forecast must be an MSM PreparedForecast")
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
            raise CacheIntegrityError(f"Invalid MSM prepared data: {exc}") from exc

    def _validate(self):
        def invalid(message):
            raise CacheIntegrityError(f"Invalid MSM prepared data: {message}")

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
                if (not allowed or hour < 0 or hour % (1 if kind == "Lsurf" else 3)
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
                        or np.any((lat < 22.4) | (lat > 47.6))
                        or np.any((lon < 120) | (lon > 150))):
                    invalid("invalid MSM grid")
                for axis in (lat[:, 0], lon[0, :]):
                    delta = np.diff(axis.astype(float, copy=False))
                    if not ((delta > 0).all() or (delta < 0).all()):
                        invalid("non-monotonic grid")
                if reference is not None and any(
                    not np.array_equal(a, b) for a, b in zip(reference, (lat, lon))
                ):
                    invalid("inconsistent product grids")
                reference = lat, lon

    def _prepare(self, selection, requirements, *, terrain_provider=None):
        """Internal assembly used by MsmClient after its ordinary Run selection."""
        from .client import MsmClient
        from .dataset import PreparedForecast
        if (selection.run_utc != self.selection.run_utc
                or any(f not in self.selection.files for f in selection.files)):
            raise CacheIntegrityError("MSM prepared data does not match selected source files")
        if not select_compatible_runs(selection.files, requirements):
            raise SelectedRunCoverageError("Selected source files do not cover the request")

        def selected_records(records, kind):
            files = tuple(remote for remote in selection.files if remote.kind == kind)
            return {key: record for key, record in records.items()
                    if any(remote.first_hour <= (key[0] - remote.run_utc).total_seconds() / 3600
                           <= remote.last_hour for remote in files)}

        surface = selected_records(self.surface, "Lsurf")
        pressure = selected_records(self.pressure, "L-pall")
        MsmClient._validate_prepared(requirements, required_valid_times(requirements),
                                     surface, pressure)
        source_hashes = {remote.url: self.source_hashes[remote.url] for remote in selection.files}
        return PreparedForecast(selection, surface, pressure,
                                source_hashes, terrain_provider=terrain_provider)

    def to_bytes(self) -> bytes:
        """Versioned NPZ snapshot, with JSON metadata and no pickle/object arrays."""
        self.validate()
        metadata = {"schema": 1, "model": "MSM", "selection": asdict(self.selection),
                    "source_hashes": self.source_hashes, "records": []}
        arrays = {}
        for group, records in (("surface", self.surface), ("pressure", self.pressure)):
            if records:
                _, lat, lon = next(iter(records.values()))
                arrays[f"{group}_latitude"] = lat
                arrays[f"{group}_longitude"] = lon
            for (valid, level, name), record in sorted(records.items()):
                index = len(metadata["records"])
                metadata["records"].append([group, valid.isoformat(), level, name])
                arrays[f"r{index}"] = np.asarray(record[0], dtype=float)
        arrays["metadata"] = np.frombuffer(
            json.dumps(metadata, default=lambda value: value.isoformat(), sort_keys=True).encode(),
            dtype=np.uint8,
        )
        output = BytesIO()
        np.savez_compressed(output, **arrays)
        return output.getvalue()

    @classmethod
    def from_bytes(cls, payload: bytes, *, expected_sha256: str | None = None):
        """Load and validate a snapshot; optionally verify its externally stored hash.

        Source hashes identify GRIB inputs; expected_sha256 identifies this payload.
        Neither a self-contained manifest nor a hash authenticates its publisher.
        Input budgets: 32 MiB compressed, 2048 members, 32 MiB per member,
        128 MiB total expanded, 1 MiB metadata. Oversize data must be partitioned
        by the producer. ZIP and NPY sizes are checked before array allocation.
        """
        if len(payload) > _MAX_PAYLOAD_BYTES:
            raise CacheIntegrityError("MSM prepared payload exceeds compressed size limit")
        if expected_sha256 is not None and hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise CacheIntegrityError("MSM prepared payload SHA-256 mismatch")
        try:
            _validate_archive(payload)
            with np.load(BytesIO(payload), allow_pickle=False) as archive:
                metadata = json.loads(archive["metadata"].tobytes())
                if metadata["schema"] != 1 or metadata["model"] != "MSM":
                    raise ValueError("unsupported schema/model")
                selection = metadata["selection"]
                files = tuple(RemoteFile(**{**item, "run_utc": datetime.fromisoformat(item["run_utc"])})
                              for item in selection["files"])
                selection = RunSelection(datetime.fromisoformat(selection["run_utc"]), files)
                groups = {"surface": {}, "pressure": {}}
                grids = {}
                for index, (group, valid, level, name) in enumerate(metadata["records"]):
                    key = (datetime.fromisoformat(valid), level, name)
                    if key in groups[group]:
                        raise ValueError("duplicate record")
                    if group not in grids:
                        grids[group] = (archive[f"{group}_latitude"], archive[f"{group}_longitude"])
                    groups[group][key] = (archive[f"r{index}"], *grids[group])
                result = cls(selection, groups["surface"], groups["pressure"], metadata["source_hashes"])
                result.validate()
                return result
        except CacheIntegrityError:
            raise
        # zipfile uses RuntimeError for encrypted members and unsupported codecs.
        except (ValueError, TypeError, KeyError, OSError, EOFError, BadZipFile,
                AttributeError, zlib.error, RuntimeError) as exc:
            raise CacheIntegrityError(f"Invalid MSM prepared payload: {exc}") from exc
