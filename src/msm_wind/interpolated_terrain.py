from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

from .cache import sha256_file
from .core import Bounds
from .errors import TerrainValidationError
from .interpolation import bilinear

SOURCE_MANIFEST_SCHEMA_VERSION = 1
CACHE_SCHEMA_VERSION = 1
TERRAIN_SOURCE_TYPE = "interpolated_msm_gpv_topography"
ARTIFACT_TYPE = "jma_msm_gpv_interpolated_topography"
MODEL_TERRAIN_VERSION = "2025-05-20"
TOPO_FILE_NAME = "TOPO.MSM_5K"
LANDSEA_FILE_NAME = "LANDSEA.MSM_5K"
GRID_NX = 481
GRID_NY = 505
GRID_POINTS = GRID_NX * GRID_NY
FILE_SIZE_BYTES = GRID_POINTS * 4
FIRST_LATITUDE = 47.6
FIRST_LONGITUDE = 120.0
LATITUDE_STEP = -0.05
LONGITUDE_STEP = 0.0625
COASTAL_LAND_FRACTION_MIN = 0.05
COASTAL_LAND_FRACTION_MAX = 0.95


def _required_mapping(value, field: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise TerrainValidationError(f"{field} must be an object")
    return value


def _required_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TerrainValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _required_int(value, field: str) -> int:
    if isinstance(value, bool):
        raise TerrainValidationError(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise TerrainValidationError(f"{field} must be an integer") from exc
    if result != value:
        raise TerrainValidationError(f"{field} must be an integer")
    return result


def _required_float(value, field: str) -> float:
    if isinstance(value, bool):
        raise TerrainValidationError(f"{field} must be a number")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise TerrainValidationError(f"{field} must be a number") from exc


def _required_bool(value, field: str) -> bool:
    if not isinstance(value, bool):
        raise TerrainValidationError(f"{field} must be a boolean")
    return value


def _validate_sha256(value: str, field: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise TerrainValidationError(
            f"{field} must contain 64 lowercase hexadecimal characters"
        )


@dataclass(frozen=True)
class InterpolatedTerrainSourceManifest:
    distribution_provider: str
    distribution_page_url: str
    distribution_archive_url: str
    distribution_archive_file_name: str
    distribution_archive_sha256: str
    inner_archive_file_name: str
    inner_archive_sha256: str
    topography_file_name: str
    topography_sha256: str
    topography_size_bytes: int
    landsea_file_name: str
    landsea_sha256: str
    landsea_size_bytes: int
    license_spdx: str
    license_url: str
    attribution: str
    source_modified: bool
    interpolated_from_model_grid: bool
    model_terrain_version: str
    model_terrain_version_basis: str
    technical_reference: str
    grid_nx: int
    grid_ny: int
    first_latitude: float
    first_longitude: float
    latitude_step: float
    longitude_step: float
    encoding: str
    byte_order: str
    storage_order: str
    schema_version: int = SOURCE_MANIFEST_SCHEMA_VERSION
    artifact_type: str = ARTIFACT_TYPE
    terrain_source_type: str = TERRAIN_SOURCE_TYPE

    @classmethod
    def load(cls, path: str | Path) -> InterpolatedTerrainSourceManifest:
        manifest_path = Path(path)
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TerrainValidationError(
                f"cannot read interpolated terrain manifest {manifest_path}: {exc}"
            ) from exc
        return cls.from_mapping(document)

    @classmethod
    def from_mapping(cls, document: Mapping) -> InterpolatedTerrainSourceManifest:
        root = _required_mapping(document, "manifest")
        distribution = _required_mapping(root.get("distribution"), "distribution")
        artifacts = _required_mapping(root.get("artifacts"), "artifacts")
        topography = _required_mapping(artifacts.get("topography"), "artifacts.topography")
        landsea = _required_mapping(artifacts.get("landsea"), "artifacts.landsea")
        license_data = _required_mapping(root.get("license"), "license")
        processing = _required_mapping(root.get("processing"), "processing")
        grid = _required_mapping(root.get("source_grid"), "source_grid")
        manifest = cls(
            schema_version=_required_int(root.get("schema_version"), "schema_version"),
            artifact_type=_required_text(root.get("artifact_type"), "artifact_type"),
            terrain_source_type=_required_text(
                root.get("terrain_source_type"), "terrain_source_type"
            ),
            model_terrain_version=_required_text(
                root.get("model_terrain_version"), "model_terrain_version"
            ),
            model_terrain_version_basis=_required_text(
                root.get("model_terrain_version_basis"),
                "model_terrain_version_basis",
            ),
            technical_reference=_required_text(
                root.get("technical_reference"), "technical_reference"
            ),
            distribution_provider=_required_text(
                distribution.get("provider"), "distribution.provider"
            ),
            distribution_page_url=_required_text(
                distribution.get("page_url"), "distribution.page_url"
            ),
            distribution_archive_url=_required_text(
                distribution.get("archive_url"), "distribution.archive_url"
            ),
            distribution_archive_file_name=_required_text(
                distribution.get("archive_file_name"),
                "distribution.archive_file_name",
            ),
            distribution_archive_sha256=_required_text(
                distribution.get("archive_sha256"), "distribution.archive_sha256"
            ).lower(),
            inner_archive_file_name=_required_text(
                distribution.get("inner_archive_file_name"),
                "distribution.inner_archive_file_name",
            ),
            inner_archive_sha256=_required_text(
                distribution.get("inner_archive_sha256"),
                "distribution.inner_archive_sha256",
            ).lower(),
            topography_file_name=_required_text(
                topography.get("file_name"), "artifacts.topography.file_name"
            ),
            topography_sha256=_required_text(
                topography.get("sha256"), "artifacts.topography.sha256"
            ).lower(),
            topography_size_bytes=_required_int(
                topography.get("size_bytes"), "artifacts.topography.size_bytes"
            ),
            landsea_file_name=_required_text(
                landsea.get("file_name"), "artifacts.landsea.file_name"
            ),
            landsea_sha256=_required_text(
                landsea.get("sha256"), "artifacts.landsea.sha256"
            ).lower(),
            landsea_size_bytes=_required_int(
                landsea.get("size_bytes"), "artifacts.landsea.size_bytes"
            ),
            license_spdx=_required_text(license_data.get("spdx"), "license.spdx"),
            license_url=_required_text(license_data.get("url"), "license.url"),
            attribution=_required_text(
                license_data.get("attribution"), "license.attribution"
            ),
            source_modified=_required_bool(
                processing.get("source_artifacts_modified"),
                "processing.source_artifacts_modified",
            ),
            interpolated_from_model_grid=_required_bool(
                processing.get("interpolated_from_model_grid"),
                "processing.interpolated_from_model_grid",
            ),
            grid_nx=_required_int(grid.get("nx"), "source_grid.nx"),
            grid_ny=_required_int(grid.get("ny"), "source_grid.ny"),
            first_latitude=_required_float(
                grid.get("first_latitude"), "source_grid.first_latitude"
            ),
            first_longitude=_required_float(
                grid.get("first_longitude"), "source_grid.first_longitude"
            ),
            latitude_step=_required_float(
                grid.get("latitude_step"), "source_grid.latitude_step"
            ),
            longitude_step=_required_float(
                grid.get("longitude_step"), "source_grid.longitude_step"
            ),
            encoding=_required_text(grid.get("encoding"), "source_grid.encoding"),
            byte_order=_required_text(
                grid.get("byte_order"), "source_grid.byte_order"
            ),
            storage_order=_required_text(
                grid.get("storage_order"), "source_grid.storage_order"
            ),
        )
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if self.schema_version != SOURCE_MANIFEST_SCHEMA_VERSION:
            raise TerrainValidationError(
                f"unsupported interpolated terrain manifest schema {self.schema_version}"
            )
        if self.artifact_type != ARTIFACT_TYPE:
            raise TerrainValidationError(f"artifact_type must be {ARTIFACT_TYPE}")
        if self.terrain_source_type != TERRAIN_SOURCE_TYPE:
            raise TerrainValidationError(
                f"terrain_source_type must be {TERRAIN_SOURCE_TYPE}"
            )
        if self.model_terrain_version != MODEL_TERRAIN_VERSION:
            raise TerrainValidationError(
                f"model_terrain_version must be {MODEL_TERRAIN_VERSION}"
            )
        if "JMBSC" not in self.distribution_provider.upper():
            raise TerrainValidationError("distribution.provider must identify JMBSC")
        for field, url in (
            ("distribution.page_url", self.distribution_page_url),
            ("distribution.archive_url", self.distribution_archive_url),
            ("license.url", self.license_url),
            ("technical_reference", self.technical_reference),
        ):
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise TerrainValidationError(f"{field} must be an HTTP(S) URL")
        for field, digest in (
            ("distribution.archive_sha256", self.distribution_archive_sha256),
            ("distribution.inner_archive_sha256", self.inner_archive_sha256),
            ("artifacts.topography.sha256", self.topography_sha256),
            ("artifacts.landsea.sha256", self.landsea_sha256),
        ):
            _validate_sha256(digest, field)
        expected = {
            "distribution_archive_file_name": "chikeidata_joho648.zip",
            "inner_archive_file_name": "202505_MSM地形データ.zip",
            "topography_file_name": TOPO_FILE_NAME,
            "landsea_file_name": LANDSEA_FILE_NAME,
            "topography_size_bytes": FILE_SIZE_BYTES,
            "landsea_size_bytes": FILE_SIZE_BYTES,
            "grid_nx": GRID_NX,
            "grid_ny": GRID_NY,
            "first_latitude": FIRST_LATITUDE,
            "first_longitude": FIRST_LONGITUDE,
            "latitude_step": LATITUDE_STEP,
            "longitude_step": LONGITUDE_STEP,
            "encoding": "IEEE754 float32",
            "byte_order": "big-endian",
            "storage_order": "row-major:north-to-south:west-to-east",
            "license_spdx": "CC-BY-4.0",
            "source_modified": False,
            "interpolated_from_model_grid": True,
        }
        for field, expected_value in expected.items():
            if getattr(self, field) != expected_value:
                raise TerrainValidationError(
                    f"{field} must be {expected_value!r}; got {getattr(self, field)!r}"
                )

    def verify_artifacts(
        self, topography_path: str | Path, landsea_path: str | Path
    ) -> None:
        self._verify_artifact(
            Path(topography_path),
            self.topography_file_name,
            self.topography_sha256,
            self.topography_size_bytes,
            "topography",
        )
        self._verify_artifact(
            Path(landsea_path),
            self.landsea_file_name,
            self.landsea_sha256,
            self.landsea_size_bytes,
            "landsea",
        )

    @staticmethod
    def _verify_artifact(
        path: Path, expected_name: str, expected_sha256: str, expected_size: int, label: str
    ) -> None:
        if path.name != expected_name:
            raise TerrainValidationError(
                f"{label} filename mismatch: expected {expected_name}, got {path.name}"
            )
        try:
            actual_size = path.stat().st_size
        except OSError as exc:
            raise TerrainValidationError(f"cannot read {label} source {path}: {exc}") from exc
        if actual_size != expected_size:
            raise TerrainValidationError(
                f"{label} size mismatch: expected {expected_size}, got {actual_size}"
            )
        actual_sha256 = sha256_file(path)
        if actual_sha256 != expected_sha256:
            raise TerrainValidationError(
                f"{label} SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
            )

    def cache_metadata(self, manifest_sha256: str) -> dict:
        return {
            "terrain_source_type": TERRAIN_SOURCE_TYPE,
            "terrain_source_name": "JMA MSM GPV interpolated model topography (TOPO.MSM_5K)",
            "terrain_source_provider": self.distribution_provider,
            "terrain_source_page_url": self.distribution_page_url,
            "terrain_source_archive_url": self.distribution_archive_url,
            "terrain_distribution_archive_file_name": self.distribution_archive_file_name,
            "terrain_distribution_archive_sha256": self.distribution_archive_sha256,
            "terrain_inner_archive_file_name": self.inner_archive_file_name,
            "terrain_inner_archive_sha256": self.inner_archive_sha256,
            "terrain_source_file_name": self.topography_file_name,
            "terrain_source_sha256": self.topography_sha256,
            "terrain_landsea_source_file_name": self.landsea_file_name,
            "terrain_landsea_source_sha256": self.landsea_sha256,
            "terrain_source_manifest_sha256": manifest_sha256,
            "terrain_model_version": self.model_terrain_version,
            "terrain_model_version_basis": self.model_terrain_version_basis,
            "terrain_license": self.license_spdx,
            "terrain_license_url": self.license_url,
            "terrain_attribution": self.attribution,
            "terrain_source_artifacts_modified": self.source_modified,
            "terrain_cache_modified": True,
            "terrain_cache_modification": "Kyushu subset with one-cell interpolation halo; NPZ compression",
            "interpolated_from_model_grid": self.interpolated_from_model_grid,
            "terrain_interpolation_method": "regular-latlon-bilinear",
            "terrain_technical_reference": self.technical_reference,
            "terrain_source_grid": {
                "projection": "regular_latitude_longitude",
                "nx": self.grid_nx,
                "ny": self.grid_ny,
                "first_latitude": self.first_latitude,
                "first_longitude": self.first_longitude,
                "latitude_step": self.latitude_step,
                "longitude_step": self.longitude_step,
                "encoding": self.encoding,
                "byte_order": self.byte_order,
                "storage_order": self.storage_order,
            },
            "terrain_coastal_land_fraction_range": [
                COASTAL_LAND_FRACTION_MIN,
                COASTAL_LAND_FRACTION_MAX,
            ],
        }


def _read_big_endian_grid(path: str | Path) -> np.ndarray:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise TerrainValidationError(f"cannot read terrain grid {path}: {exc}") from exc
    if len(raw) != FILE_SIZE_BYTES:
        raise TerrainValidationError(
            f"terrain grid size mismatch: expected {FILE_SIZE_BYTES}, got {len(raw)}"
        )
    values = np.frombuffer(raw, dtype=">f4")
    if values.size != GRID_POINTS:
        raise TerrainValidationError(
            f"terrain grid must contain {GRID_POINTS} float32 values"
        )
    return values.astype(np.float64).reshape(GRID_NY, GRID_NX)


def _validate_source_values(topography: np.ndarray, land_fraction: np.ndarray) -> None:
    if topography.shape != (GRID_NY, GRID_NX) or land_fraction.shape != (GRID_NY, GRID_NX):
        raise TerrainValidationError("interpolated terrain source shape must be 505x481")
    if not np.isfinite(topography).all() or not np.isfinite(land_fraction).all():
        raise TerrainValidationError("interpolated terrain source contains non-finite values")
    minimum_topography = float(np.min(topography))
    maximum_topography = float(np.max(topography))
    if minimum_topography < -1000 or maximum_topography > 10000 or maximum_topography < 100:
        raise TerrainValidationError(
            "topography values are physically implausible; verify big-endian float32 encoding"
        )
    minimum_land = float(np.min(land_fraction))
    maximum_land = float(np.max(land_fraction))
    if minimum_land < -1e-6 or maximum_land > 1 + 1e-6:
        raise TerrainValidationError("LANDSEA values must be fractions between 0 and 1")
    if minimum_land > 0.01 or maximum_land < 0.99:
        raise TerrainValidationError(
            "LANDSEA does not contain expected water/land values; verify big-endian encoding"
        )


def _source_axes() -> tuple[np.ndarray, np.ndarray]:
    latitudes = FIRST_LATITUDE + np.arange(GRID_NY, dtype=float) * LATITUDE_STEP
    longitudes = FIRST_LONGITUDE + np.arange(GRID_NX, dtype=float) * LONGITUDE_STEP
    return latitudes, longitudes


def _subset_with_halo(
    topography: np.ndarray, land_fraction: np.ndarray, bounds: Bounds
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    latitudes, longitudes = _source_axes()
    rows = np.flatnonzero(
        (latitudes >= bounds.lat_min) & (latitudes <= bounds.lat_max)
    )
    columns = np.flatnonzero(
        (longitudes >= bounds.lon_min) & (longitudes <= bounds.lon_max)
    )
    if rows.size == 0 or columns.size == 0:
        raise TerrainValidationError("TOPO.MSM_5K does not cover requested bounds")
    row_start = max(int(rows[0]) - 1, 0)
    row_stop = min(int(rows[-1]) + 2, GRID_NY)
    column_start = max(int(columns[0]) - 1, 0)
    column_stop = min(int(columns[-1]) + 2, GRID_NX)
    return (
        topography[row_start:row_stop, column_start:column_stop],
        land_fraction[row_start:row_stop, column_start:column_stop],
        latitudes[row_start:row_stop],
        longitudes[column_start:column_stop],
    )


def _axis_bracket(axis: np.ndarray, target: float):
    ascending = axis if axis[0] < axis[-1] else axis[::-1]
    if target < ascending[0] or target > ascending[-1]:
        return None
    position = int(np.searchsorted(ascending, target, side="left"))
    if position == 0:
        lower, upper = 0, 1
    elif position == len(ascending):
        lower, upper = len(ascending) - 2, len(ascending) - 1
    else:
        lower, upper = position - 1, position
    if axis[0] < axis[-1]:
        indices = (lower, upper)
    else:
        indices = (len(axis) - 1 - lower, len(axis) - 1 - upper)
    return indices, (float(ascending[lower]), float(ascending[upper]))


@dataclass
class InterpolatedMsmTopographyProvider:
    topography_m: np.ndarray
    land_fraction: np.ndarray
    latitudes: np.ndarray
    longitudes: np.ndarray
    metadata: Mapping

    @classmethod
    def from_raw(
        cls,
        topography_path: str | Path,
        landsea_path: str | Path,
        source_manifest: str | Path,
        bounds: Bounds = Bounds(),
    ) -> InterpolatedMsmTopographyProvider:
        manifest_path = Path(source_manifest)
        manifest = InterpolatedTerrainSourceManifest.load(manifest_path)
        manifest.verify_artifacts(topography_path, landsea_path)
        topography = _read_big_endian_grid(topography_path)
        land_fraction = _read_big_endian_grid(landsea_path)
        _validate_source_values(topography, land_fraction)
        subset = _subset_with_halo(topography, land_fraction, bounds)
        provider = cls(
            *subset,
            metadata=manifest.cache_metadata(sha256_file(manifest_path)),
        )
        provider._validate_cache()
        return provider

    @classmethod
    def load(cls, path: str | Path) -> InterpolatedMsmTopographyProvider:
        try:
            with np.load(path) as data:
                raw_metadata = data["metadata"].item()
                if isinstance(raw_metadata, bytes):
                    raw_metadata = raw_metadata.decode("utf-8")
                provider = cls(
                    np.asarray(data["topography_m"], dtype=float),
                    np.asarray(data["land_fraction"], dtype=float),
                    np.asarray(data["latitudes"], dtype=float),
                    np.asarray(data["longitudes"], dtype=float),
                    json.loads(str(raw_metadata)),
                )
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise TerrainValidationError(
                f"cannot load interpolated terrain cache {path}: {exc}"
            ) from exc
        provider._validate_cache()
        return provider

    def _validate_cache(self) -> None:
        topography = np.asarray(self.topography_m, dtype=float)
        land_fraction = np.asarray(self.land_fraction, dtype=float)
        latitudes = np.asarray(self.latitudes, dtype=float)
        longitudes = np.asarray(self.longitudes, dtype=float)
        if topography.shape != land_fraction.shape:
            raise TerrainValidationError("topography and LANDSEA cache shapes differ")
        if topography.ndim != 2 or min(topography.shape) < 2:
            raise TerrainValidationError("interpolated terrain cache must be a 2-D grid")
        if topography.shape != (len(latitudes), len(longitudes)):
            raise TerrainValidationError("interpolated terrain cache axes do not match data")
        if not all(np.isfinite(value).all() for value in (topography, land_fraction, latitudes, longitudes)):
            raise TerrainValidationError("interpolated terrain cache contains non-finite values")
        if not np.all(np.diff(latitudes) < 0) or not np.all(np.diff(longitudes) > 0):
            raise TerrainValidationError(
                "interpolated terrain cache must be north-to-south and west-to-east"
            )
        if not np.allclose(np.diff(latitudes), LATITUDE_STEP) or not np.allclose(
            np.diff(longitudes), LONGITUDE_STEP
        ):
            raise TerrainValidationError("interpolated terrain cache grid spacing is invalid")
        if np.min(land_fraction) < -1e-6 or np.max(land_fraction) > 1 + 1e-6:
            raise TerrainValidationError("cached LANDSEA values must be between 0 and 1")
        if self.metadata.get("terrain_source_type") != TERRAIN_SOURCE_TYPE:
            raise TerrainValidationError(
                "cache is not an interpolated MSM GPV topography cache"
            )
        if self.metadata.get("terrain_model_version") != MODEL_TERRAIN_VERSION:
            raise TerrainValidationError("interpolated terrain cache model version is unsupported")
        if self.metadata.get("terrain_license") != "CC-BY-4.0":
            raise TerrainValidationError("interpolated terrain cache must retain CC BY 4.0")
        if self.metadata.get("interpolated_from_model_grid") is not True:
            raise TerrainValidationError("cache must identify model-grid interpolation")
        for field in (
            "terrain_distribution_archive_sha256",
            "terrain_inner_archive_sha256",
            "terrain_source_sha256",
            "terrain_landsea_source_sha256",
            "terrain_source_manifest_sha256",
        ):
            _validate_sha256(str(self.metadata.get(field, "")), field)

    def save(self, path: str | Path) -> Path:
        self._validate_cache()
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            **dict(self.metadata),
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "cache_grid": {
                "shape": [int(value) for value in self.topography_m.shape],
                "first_latitude": float(self.latitudes[0]),
                "first_longitude": float(self.longitudes[0]),
                "latitude_step": LATITUDE_STEP,
                "longitude_step": LONGITUDE_STEP,
            },
        }
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open("wb") as handle:
            np.savez_compressed(
                handle,
                topography_m=self.topography_m,
                land_fraction=self.land_fraction,
                latitudes=self.latitudes,
                longitudes=self.longitudes,
                metadata=json.dumps(metadata, sort_keys=True, ensure_ascii=False),
            )
        temporary.replace(destination)
        sidecar = destination.with_suffix(destination.suffix + ".json")
        sidecar_temporary = sidecar.with_suffix(sidecar.suffix + ".tmp")
        sidecar_temporary.write_text(
            json.dumps(
                {**metadata, "terrain_cache": destination.name},
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        sidecar_temporary.replace(sidecar)
        return destination

    def _interpolate(self, values: np.ndarray, latitude: float, longitude: float):
        latitude_bracket = _axis_bracket(np.asarray(self.latitudes), latitude)
        longitude_bracket = _axis_bracket(np.asarray(self.longitudes), longitude)
        if latitude_bracket is None or longitude_bracket is None:
            return None
        yi, latitude_pair = latitude_bracket
        xi, longitude_pair = longitude_bracket
        corners = np.array(
            [
                [values[yi[0], xi[0]], values[yi[0], xi[1]]],
                [values[yi[1], xi[0]], values[yi[1], xi[1]]],
            ],
            dtype=float,
        )
        if not np.isfinite(corners).all():
            return None
        return float(
            bilinear(
                longitude,
                latitude,
                *longitude_pair,
                *latitude_pair,
                corners,
            )
        )

    def __call__(self, latitude: float, longitude: float) -> float | None:
        return self._interpolate(self.topography_m, latitude, longitude)

    def land_fraction_at(self, latitude: float, longitude: float) -> float | None:
        value = self._interpolate(self.land_fraction, latitude, longitude)
        return None if value is None else min(max(value, 0.0), 1.0)

    def qnh_diagnostics(self, latitude: float, longitude: float) -> dict:
        land_fraction = self.land_fraction_at(latitude, longitude)
        coastal = bool(
            land_fraction is not None
            and COASTAL_LAND_FRACTION_MIN < land_fraction < COASTAL_LAND_FRACTION_MAX
        )
        return {
            "terrain_land_fraction": land_fraction,
            "terrain_coastal_mixed_fraction": coastal,
        }

    def qnh_warnings(self, latitude: float, longitude: float) -> tuple[str, ...]:
        diagnostics = self.qnh_diagnostics(latitude, longitude)
        warnings = ["INTERPOLATED_MODEL_TERRAIN"]
        if diagnostics["terrain_coastal_mixed_fraction"]:
            warnings.append("COASTAL_MIXED_LAND_FRACTION")
        return tuple(warnings)

    @property
    def source(self) -> str:
        return str(self.metadata.get("terrain_source_name"))

    @property
    def source_sha256(self) -> str | None:
        value = self.metadata.get("terrain_source_sha256")
        return None if value is None else str(value)

    @property
    def provenance(self) -> dict:
        return dict(self.metadata)
