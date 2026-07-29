from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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
MODEL_TERRAIN_VERSION_BASIS = (
    "JMA implementation notice: MSM changes including updated model terrain "
    "apply from the 2025-05-20 00UTC initial run "
    "(https://www.data.jma.go.jp/suishin/oshirase/pdf/20250509.pdf)"
)
TECHNICAL_REFERENCE = "https://www.data.jma.go.jp/suishin/jyouhou/pdf/648.pdf"
DISTRIBUTION_PROVIDER = "Japan Meteorological Business Support Center (JMBSC)"
DISTRIBUTION_PAGE_URL = "https://www.jmbsc.or.jp/jp/online/c-onlineGsd.html"
DISTRIBUTION_ARCHIVE_URL = (
    "https://www.jmbsc-west.jp/jp/online/online-sample/joho-sample/"
    "chikeidata_joho648.zip"
)
DISTRIBUTION_ARCHIVE_FILE_NAME = "chikeidata_joho648.zip"
DISTRIBUTION_ARCHIVE_SHA256 = (
    "6251a2494d8ac0ce6a26ee7c8a8dabc854010c5e9173d5b963ba4880791d242e"
)
INNER_ARCHIVE_FILE_NAME = "202505_MSM地形データ.zip"
INNER_ARCHIVE_SHA256 = (
    "06f678659f8d01b7fc44fe3736da51d78cd398358eca51a34dedea8c9eec75bb"
)
INNER_DIRECTORY = "202505_MSM地形データ"
TOPO_FILE_NAME = "TOPO.MSM_5K"
TOPO_SHA256 = "6ce16ae3781399dad2d618220d81fc41938aa54d693174c33ba347ad976f5250"
LANDSEA_FILE_NAME = "LANDSEA.MSM_5K"
LANDSEA_SHA256 = "322bbb1a4086174f7ac813accc391118a1fcd7d13c3135878f52e716f6870483"
LICENSE_SPDX = "CC-BY-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
LICENSE_SOURCE_FILE_NAME = "202505_MSM地形データ/README.txt"
LICENSE_SOURCE_DATE = "令和7年4月"
LICENSE_ISSUER = "気象庁"
ATTRIBUTION = (
    "東京大学准教授 山崎大氏作成の MERIT-DEM を気象庁が低解像度化した"
    "地形データ（CC BY 4.0）。生成する cache は九州範囲への切り出しと "
    "NPZ 圧縮を行った加工物です。"
)
GRID_NX = 481
GRID_NY = 505
GRID_POINTS = GRID_NX * GRID_NY
FILE_SIZE_BYTES = GRID_POINTS * 4
FIRST_LATITUDE = 47.6
FIRST_LONGITUDE = 120.0
LATITUDE_STEP = -0.05
LONGITUDE_STEP = 0.0625
STORAGE_ORDER = "row-major:north-to-south:west-to-east"
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


def _verify_bytes(
    raw: bytes, expected_sha256: str, label: str, expected_size: int | None = None
) -> None:
    if expected_size is not None and len(raw) != expected_size:
        raise TerrainValidationError(
            f"{label} size mismatch: expected {expected_size}, got {len(raw)}"
        )
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise TerrainValidationError(
            f"{label} SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
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
    license_source_file_name: str
    license_source_date: str
    license_issuer: str
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
        topography = _required_mapping(
            artifacts.get("topography"), "artifacts.topography"
        )
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
            license_source_file_name=_required_text(
                license_data.get("source_file_name"), "license.source_file_name"
            ),
            license_source_date=_required_text(
                license_data.get("source_date"), "license.source_date"
            ),
            license_issuer=_required_text(
                license_data.get("issuer"), "license.issuer"
            ),
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
        expected = {
            "schema_version": SOURCE_MANIFEST_SCHEMA_VERSION,
            "artifact_type": ARTIFACT_TYPE,
            "terrain_source_type": TERRAIN_SOURCE_TYPE,
            "model_terrain_version": MODEL_TERRAIN_VERSION,
            "model_terrain_version_basis": MODEL_TERRAIN_VERSION_BASIS,
            "technical_reference": TECHNICAL_REFERENCE,
            "distribution_provider": DISTRIBUTION_PROVIDER,
            "distribution_page_url": DISTRIBUTION_PAGE_URL,
            "distribution_archive_url": DISTRIBUTION_ARCHIVE_URL,
            "distribution_archive_file_name": DISTRIBUTION_ARCHIVE_FILE_NAME,
            "distribution_archive_sha256": DISTRIBUTION_ARCHIVE_SHA256,
            "inner_archive_file_name": INNER_ARCHIVE_FILE_NAME,
            "inner_archive_sha256": INNER_ARCHIVE_SHA256,
            "topography_file_name": TOPO_FILE_NAME,
            "topography_sha256": TOPO_SHA256,
            "topography_size_bytes": FILE_SIZE_BYTES,
            "landsea_file_name": LANDSEA_FILE_NAME,
            "landsea_sha256": LANDSEA_SHA256,
            "landsea_size_bytes": FILE_SIZE_BYTES,
            "license_spdx": LICENSE_SPDX,
            "license_url": LICENSE_URL,
            "license_source_file_name": LICENSE_SOURCE_FILE_NAME,
            "license_source_date": LICENSE_SOURCE_DATE,
            "license_issuer": LICENSE_ISSUER,
            "attribution": ATTRIBUTION,
            "source_modified": False,
            "interpolated_from_model_grid": True,
            "grid_nx": GRID_NX,
            "grid_ny": GRID_NY,
            "first_latitude": FIRST_LATITUDE,
            "first_longitude": FIRST_LONGITUDE,
            "latitude_step": LATITUDE_STEP,
            "longitude_step": LONGITUDE_STEP,
            "encoding": "IEEE754 float32",
            "byte_order": "big-endian",
            "storage_order": STORAGE_ORDER,
        }
        for field, expected_value in expected.items():
            if getattr(self, field) != expected_value:
                raise TerrainValidationError(
                    f"{field} must match the pinned official release: "
                    f"expected {expected_value!r}, got {getattr(self, field)!r}"
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
        path: Path,
        expected_name: str,
        expected_sha256: str,
        expected_size: int,
        label: str,
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

    def read_distribution_archive(
        self, archive_path: str | Path
    ) -> tuple[bytes, bytes]:
        archive = Path(archive_path)
        if archive.name != self.distribution_archive_file_name:
            raise TerrainValidationError(
                "distribution archive filename mismatch: expected "
                f"{self.distribution_archive_file_name}, got {archive.name}"
            )
        try:
            archive_bytes = archive.read_bytes()
        except OSError as exc:
            raise TerrainValidationError(
                f"cannot read terrain distribution archive {archive}: {exc}"
            ) from exc
        _verify_bytes(
            archive_bytes,
            self.distribution_archive_sha256,
            "distribution archive",
        )
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as outer:
                inner_bytes = outer.read(self.inner_archive_file_name)
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            raise TerrainValidationError(
                f"cannot read inner terrain archive: {exc}"
            ) from exc
        _verify_bytes(inner_bytes, self.inner_archive_sha256, "inner archive")
        try:
            with zipfile.ZipFile(io.BytesIO(inner_bytes)) as inner:
                topography_bytes = inner.read(
                    f"{INNER_DIRECTORY}/{self.topography_file_name}"
                )
                landsea_bytes = inner.read(
                    f"{INNER_DIRECTORY}/{self.landsea_file_name}"
                )
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            raise TerrainValidationError(
                f"cannot read terrain artifacts from inner archive: {exc}"
            ) from exc
        _verify_bytes(
            topography_bytes,
            self.topography_sha256,
            "topography",
            self.topography_size_bytes,
        )
        _verify_bytes(
            landsea_bytes,
            self.landsea_sha256,
            "landsea",
            self.landsea_size_bytes,
        )
        return topography_bytes, landsea_bytes

    def cache_metadata(
        self, manifest_sha256: str, distribution_chain_verified: bool
    ) -> dict:
        return {
            "terrain_source_type": TERRAIN_SOURCE_TYPE,
            "terrain_source_name": (
                "JMA MSM GPV interpolated model topography (TOPO.MSM_5K)"
            ),
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
            "terrain_distribution_chain_verified": distribution_chain_verified,
            "terrain_artifacts_verified": True,
            "terrain_distribution_validation": (
                "pinned official archive digests and direct artifact SHA-256"
            ),
            "terrain_model_version": self.model_terrain_version,
            "terrain_model_version_basis": self.model_terrain_version_basis,
            "terrain_license": self.license_spdx,
            "terrain_license_url": self.license_url,
            "terrain_license_source_file_name": self.license_source_file_name,
            "terrain_license_source_date": self.license_source_date,
            "terrain_license_issuer": self.license_issuer,
            "terrain_attribution": self.attribution,
            "terrain_source_artifacts_modified": self.source_modified,
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


def _read_big_endian_grid(raw: bytes, label: str) -> np.ndarray:
    if len(raw) != FILE_SIZE_BYTES:
        raise TerrainValidationError(
            f"{label} size mismatch: expected {FILE_SIZE_BYTES}, got {len(raw)}"
        )
    values = np.frombuffer(raw, dtype=">f4")
    if values.size != GRID_POINTS:
        raise TerrainValidationError(
            f"{label} must contain {GRID_POINTS} float32 values"
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


def _enclosing_slice(axis: np.ndarray, lower: float, upper: float) -> slice:
    ascending = axis if axis[0] < axis[-1] else axis[::-1]
    lower_index = int(np.searchsorted(ascending, lower, side="right")) - 1
    upper_index = int(np.searchsorted(ascending, upper, side="left"))
    lower_index = max(lower_index, 0)
    upper_index = min(upper_index, len(ascending) - 1)
    halo_lower = max(lower_index - 1, 0)
    halo_upper = min(upper_index + 1, len(ascending) - 1)
    if axis[0] < axis[-1]:
        first, last = halo_lower, halo_upper
    else:
        first = len(axis) - 1 - halo_upper
        last = len(axis) - 1 - halo_lower
    return slice(first, last + 1)


def _subset_with_halo(
    topography: np.ndarray, land_fraction: np.ndarray, bounds: Bounds
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    latitudes, longitudes = _source_axes()
    if bounds.lat_min > bounds.lat_max or bounds.lon_min > bounds.lon_max:
        raise TerrainValidationError("requested terrain bounds are invalid")
    if (
        bounds.lat_min < float(np.min(latitudes))
        or bounds.lat_max > float(np.max(latitudes))
        or bounds.lon_min < float(np.min(longitudes))
        or bounds.lon_max > float(np.max(longitudes))
    ):
        raise TerrainValidationError("TOPO.MSM_5K does not cover requested bounds")
    rows = _enclosing_slice(latitudes, bounds.lat_min, bounds.lat_max)
    columns = _enclosing_slice(longitudes, bounds.lon_min, bounds.lon_max)
    return (
        topography[rows, columns],
        land_fraction[rows, columns],
        latitudes[rows],
        longitudes[columns],
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


def _subset_metadata(
    metadata: Mapping,
    bounds: Bounds,
    topography: np.ndarray,
    latitudes: np.ndarray,
    longitudes: np.ndarray,
) -> dict:
    subset_modified = topography.shape != (GRID_NY, GRID_NX)
    subset_description = (
        "Subset to requested bounds with one-cell interpolation halo"
        if subset_modified
        else "none"
    )
    return {
        **dict(metadata),
        "terrain_subset_requested_bounds": {
            "lat_min": bounds.lat_min,
            "lat_max": bounds.lat_max,
            "lon_min": bounds.lon_min,
            "lon_max": bounds.lon_max,
        },
        "terrain_subset_grid": {
            "shape": [int(value) for value in topography.shape],
            "first_latitude": float(latitudes[0]),
            "last_latitude": float(latitudes[-1]),
            "first_longitude": float(longitudes[0]),
            "last_longitude": float(longitudes[-1]),
            "latitude_step": LATITUDE_STEP,
            "longitude_step": LONGITUDE_STEP,
        },
        "terrain_subset_modified": subset_modified,
        "terrain_subset_modification": subset_description,
        "terrain_cache_modified": subset_modified,
        "terrain_cache_modification": subset_description,
        "terrain_cache_serialized": False,
    }


@dataclass
class InterpolatedMsmTopographyProvider:
    topography_m: np.ndarray
    land_fraction: np.ndarray
    latitudes: np.ndarray
    longitudes: np.ndarray
    metadata: Mapping

    @classmethod
    def from_distribution_archive(
        cls,
        archive_path: str | Path,
        source_manifest: str | Path,
        bounds: Bounds = Bounds(),
    ) -> InterpolatedMsmTopographyProvider:
        manifest_path = Path(source_manifest)
        manifest = InterpolatedTerrainSourceManifest.load(manifest_path)
        topography_bytes, landsea_bytes = manifest.read_distribution_archive(
            archive_path
        )
        return cls._from_source_bytes(
            topography_bytes,
            landsea_bytes,
            manifest,
            manifest_path,
            bounds,
            distribution_chain_verified=True,
        )

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
        try:
            topography_bytes = Path(topography_path).read_bytes()
            landsea_bytes = Path(landsea_path).read_bytes()
        except OSError as exc:
            raise TerrainValidationError(f"cannot read terrain artifacts: {exc}") from exc
        return cls._from_source_bytes(
            topography_bytes,
            landsea_bytes,
            manifest,
            manifest_path,
            bounds,
            distribution_chain_verified=False,
        )

    @classmethod
    def _from_source_bytes(
        cls,
        topography_bytes: bytes,
        landsea_bytes: bytes,
        manifest: InterpolatedTerrainSourceManifest,
        manifest_path: Path,
        bounds: Bounds,
        distribution_chain_verified: bool,
    ) -> InterpolatedMsmTopographyProvider:
        topography = _read_big_endian_grid(topography_bytes, "topography")
        land_fraction = _read_big_endian_grid(landsea_bytes, "landsea")
        _validate_source_values(topography, land_fraction)
        subset = _subset_with_halo(topography, land_fraction, bounds)
        metadata = manifest.cache_metadata(
            sha256_file(manifest_path), distribution_chain_verified
        )
        provider = cls(
            *subset,
            metadata=_subset_metadata(
                metadata, bounds, subset[0], subset[2], subset[3]
            ),
        )
        provider._validate_cache(require_serialized=False)
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
        provider._validate_cache(require_serialized=True)
        return provider

    def _validate_cache(self, require_serialized: bool) -> None:
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
        if not all(
            np.isfinite(value).all()
            for value in (topography, land_fraction, latitudes, longitudes)
        ):
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

        exact_metadata = {
            "terrain_source_type": TERRAIN_SOURCE_TYPE,
            "terrain_source_name": (
                "JMA MSM GPV interpolated model topography (TOPO.MSM_5K)"
            ),
            "terrain_source_provider": DISTRIBUTION_PROVIDER,
            "terrain_source_page_url": DISTRIBUTION_PAGE_URL,
            "terrain_source_archive_url": DISTRIBUTION_ARCHIVE_URL,
            "terrain_distribution_archive_file_name": DISTRIBUTION_ARCHIVE_FILE_NAME,
            "terrain_distribution_archive_sha256": DISTRIBUTION_ARCHIVE_SHA256,
            "terrain_inner_archive_file_name": INNER_ARCHIVE_FILE_NAME,
            "terrain_inner_archive_sha256": INNER_ARCHIVE_SHA256,
            "terrain_source_file_name": TOPO_FILE_NAME,
            "terrain_source_sha256": TOPO_SHA256,
            "terrain_landsea_source_file_name": LANDSEA_FILE_NAME,
            "terrain_landsea_source_sha256": LANDSEA_SHA256,
            "terrain_distribution_validation": (
                "pinned official archive digests and direct artifact SHA-256"
            ),
            "terrain_model_version": MODEL_TERRAIN_VERSION,
            "terrain_model_version_basis": MODEL_TERRAIN_VERSION_BASIS,
            "terrain_license": LICENSE_SPDX,
            "terrain_license_url": LICENSE_URL,
            "terrain_license_source_file_name": LICENSE_SOURCE_FILE_NAME,
            "terrain_license_source_date": LICENSE_SOURCE_DATE,
            "terrain_license_issuer": LICENSE_ISSUER,
            "terrain_attribution": ATTRIBUTION,
            "terrain_source_artifacts_modified": False,
            "interpolated_from_model_grid": True,
            "terrain_interpolation_method": "regular-latlon-bilinear",
            "terrain_technical_reference": TECHNICAL_REFERENCE,
            "terrain_coastal_land_fraction_range": [
                COASTAL_LAND_FRACTION_MIN,
                COASTAL_LAND_FRACTION_MAX,
            ],
        }
        for field, expected in exact_metadata.items():
            if self.metadata.get(field) != expected:
                raise TerrainValidationError(
                    f"interpolated terrain cache {field} is missing or invalid"
                )
        for field in (
            "terrain_source_manifest_sha256",
            "terrain_distribution_archive_sha256",
            "terrain_inner_archive_sha256",
            "terrain_source_sha256",
            "terrain_landsea_source_sha256",
        ):
            _validate_sha256(str(self.metadata.get(field, "")), field)
        for field in (
            "terrain_distribution_chain_verified",
            "terrain_artifacts_verified",
            "terrain_subset_modified",
            "terrain_cache_modified",
            "terrain_cache_serialized",
        ):
            _required_bool(self.metadata.get(field), field)
        if self.metadata.get("terrain_artifacts_verified") is not True:
            raise TerrainValidationError("terrain artifacts must be verified")

        source_grid = _required_mapping(
            self.metadata.get("terrain_source_grid"), "terrain_source_grid"
        )
        expected_source_grid = {
            "projection": "regular_latitude_longitude",
            "nx": GRID_NX,
            "ny": GRID_NY,
            "first_latitude": FIRST_LATITUDE,
            "first_longitude": FIRST_LONGITUDE,
            "latitude_step": LATITUDE_STEP,
            "longitude_step": LONGITUDE_STEP,
            "encoding": "IEEE754 float32",
            "byte_order": "big-endian",
            "storage_order": STORAGE_ORDER,
        }
        if dict(source_grid) != expected_source_grid:
            raise TerrainValidationError("terrain source grid provenance is invalid")

        requested = _required_mapping(
            self.metadata.get("terrain_subset_requested_bounds"),
            "terrain_subset_requested_bounds",
        )
        for field in ("lat_min", "lat_max", "lon_min", "lon_max"):
            _required_float(requested.get(field), f"terrain_subset_requested_bounds.{field}")
        if (
            requested["lat_min"] > requested["lat_max"]
            or requested["lon_min"] > requested["lon_max"]
            or requested["lat_min"] < float(np.min(latitudes))
            or requested["lat_max"] > float(np.max(latitudes))
            or requested["lon_min"] < float(np.min(longitudes))
            or requested["lon_max"] > float(np.max(longitudes))
        ):
            raise TerrainValidationError("terrain subset does not cover requested bounds")

        subset_grid = _required_mapping(
            self.metadata.get("terrain_subset_grid"), "terrain_subset_grid"
        )
        expected_subset_grid = {
            "shape": [int(value) for value in topography.shape],
            "first_latitude": float(latitudes[0]),
            "last_latitude": float(latitudes[-1]),
            "first_longitude": float(longitudes[0]),
            "last_longitude": float(longitudes[-1]),
            "latitude_step": LATITUDE_STEP,
            "longitude_step": LONGITUDE_STEP,
        }
        if dict(subset_grid) != expected_subset_grid:
            raise TerrainValidationError("terrain subset grid metadata does not match arrays")
        _required_text(
            self.metadata.get("terrain_subset_modification"),
            "terrain_subset_modification",
        )
        _required_text(
            self.metadata.get("terrain_cache_modification"),
            "terrain_cache_modification",
        )

        if require_serialized:
            if self.metadata.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
                raise TerrainValidationError("unsupported interpolated terrain cache schema")
            if self.metadata.get("terrain_cache_serialized") is not True:
                raise TerrainValidationError("terrain cache serialization provenance is missing")
            if self.metadata.get("terrain_cache_serialization") != "NPZ compression":
                raise TerrainValidationError("terrain cache serialization is invalid")
            cache_grid = _required_mapping(
                self.metadata.get("cache_grid"), "cache_grid"
            )
            if dict(cache_grid) != expected_subset_grid:
                raise TerrainValidationError("cache grid metadata does not match arrays")

    def save(self, path: str | Path) -> Path:
        self._validate_cache(require_serialized=False)
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        operations = []
        if self.metadata.get("terrain_subset_modified"):
            operations.append(str(self.metadata["terrain_subset_modification"]))
        operations.append("NPZ compression")
        cache_grid = {
            "shape": [int(value) for value in self.topography_m.shape],
            "first_latitude": float(self.latitudes[0]),
            "last_latitude": float(self.latitudes[-1]),
            "first_longitude": float(self.longitudes[0]),
            "last_longitude": float(self.longitudes[-1]),
            "latitude_step": LATITUDE_STEP,
            "longitude_step": LONGITUDE_STEP,
        }
        metadata = {
            **dict(self.metadata),
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "cache_grid": cache_grid,
            "terrain_cache_modified": True,
            "terrain_cache_modification": "; ".join(operations),
            "terrain_cache_serialized": True,
            "terrain_cache_serialization": "NPZ compression",
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
        self.metadata = metadata
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
