from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

from .cache import sha256_file, validate_grib
from .core import Bounds
from .errors import TerrainValidationError

UTC = timezone.utc
SOURCE_MANIFEST_SCHEMA_VERSION = 1
TERRAIN_CACHE_SCHEMA_VERSION = 2
SUPPORTED_MODEL_TERRAIN_VERSION = "2025-05-20"
PZS_ARTIFACT_TYPE = "jma_msm_gpv_model_terrain_pzs"
PZS_GRID_NX = 817
PZS_GRID_NY = 661
PZS_GRID_POINTS = PZS_GRID_NX * PZS_GRID_NY
PZS_FILENAME_RE = re.compile(
    r"^Z__C_RJTD_(?P<initial>\d{14})_MSM_GPV_Rjp_"
    r"Glm5km_Lm1-39_Pzs_FH00_grib2\.bin$"
)
TECHNICAL_REFERENCE_619 = (
    "https://www.data.jma.go.jp/suishin/jyouhou/pdf/619.pdf"
)
TECHNICAL_REFERENCE_648 = (
    "https://www.data.jma.go.jp/suishin/jyouhou/pdf/648.pdf"
)


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _required_mapping(value, field: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise TerrainValidationError(f"{field} must be an object")
    return value


def _required_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TerrainValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _parse_utc(value, field: str) -> datetime:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TerrainValidationError(f"{field} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise TerrainValidationError(f"{field} must use UTC")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class TerrainSourceManifest:
    file_name: str
    sha256: str
    initial_time_utc: datetime
    model_terrain_version: str
    source_kind: str
    source_provider: str
    source_url: str
    acquired_at_utc: datetime
    private_repository_bundling: str
    usage_terms: str
    redistribution_terms: str
    terms_reference: str
    technical_references: tuple[str, ...]
    schema_version: int = SOURCE_MANIFEST_SCHEMA_VERSION
    artifact_type: str = PZS_ARTIFACT_TYPE

    @classmethod
    def load(cls, path: str | Path) -> TerrainSourceManifest:
        manifest_path = Path(path)
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TerrainValidationError(
                f"cannot read terrain source manifest {manifest_path}: {exc}"
            ) from exc
        return cls.from_mapping(document)

    @classmethod
    def from_mapping(cls, document: Mapping) -> TerrainSourceManifest:
        root = _required_mapping(document, "manifest")
        source = _required_mapping(root.get("source"), "source")
        terms = _required_mapping(root.get("terms"), "terms")
        references = root.get("technical_references")
        if (
            not isinstance(references, Sequence)
            or isinstance(references, (str, bytes))
            or not references
        ):
            raise TerrainValidationError(
                "technical_references must be a non-empty array"
            )
        try:
            schema_version = int(root.get("schema_version"))
        except (TypeError, ValueError) as exc:
            raise TerrainValidationError(
                "schema_version must be an integer"
            ) from exc
        manifest = cls(
            schema_version=schema_version,
            artifact_type=_required_text(root.get("artifact_type"), "artifact_type"),
            file_name=_required_text(root.get("file_name"), "file_name"),
            sha256=_required_text(root.get("sha256"), "sha256").lower(),
            initial_time_utc=_parse_utc(
                root.get("initial_time_utc"), "initial_time_utc"
            ),
            model_terrain_version=_required_text(
                root.get("model_terrain_version"), "model_terrain_version"
            ),
            source_kind=_required_text(source.get("kind"), "source.kind"),
            source_provider=_required_text(
                source.get("provider"), "source.provider"
            ),
            source_url=_required_text(source.get("url"), "source.url"),
            acquired_at_utc=_parse_utc(
                source.get("acquired_at_utc"), "source.acquired_at_utc"
            ),
            private_repository_bundling=_required_text(
                terms.get("private_repository_bundling"),
                "terms.private_repository_bundling",
            ),
            usage_terms=_required_text(terms.get("usage"), "terms.usage"),
            redistribution_terms=_required_text(
                terms.get("redistribution"), "terms.redistribution"
            ),
            terms_reference=_required_text(
                terms.get("reference"), "terms.reference"
            ),
            technical_references=tuple(
                _required_text(value, "technical_references[]")
                for value in references
            ),
        )
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if self.schema_version != SOURCE_MANIFEST_SCHEMA_VERSION:
            raise TerrainValidationError(
                "unsupported terrain source manifest schema_version "
                f"{self.schema_version}"
            )
        if self.artifact_type != PZS_ARTIFACT_TYPE:
            raise TerrainValidationError(
                f"artifact_type must be {PZS_ARTIFACT_TYPE}"
            )
        match = PZS_FILENAME_RE.fullmatch(self.file_name)
        if match is None:
            raise TerrainValidationError(
                "file_name is not an MSM Pzs FH00 GRIB2 filename"
            )
        if re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise TerrainValidationError(
                "sha256 must contain 64 lowercase hexadecimal characters"
            )
        filename_time = datetime.strptime(
            match.group("initial"), "%Y%m%d%H%M%S"
        ).replace(tzinfo=UTC)
        if filename_time != self.initial_time_utc:
            raise TerrainValidationError(
                "initial_time_utc does not match the Pzs filename"
            )
        if self.model_terrain_version != SUPPORTED_MODEL_TERRAIN_VERSION:
            raise TerrainValidationError(
                "unsupported model_terrain_version "
                f"{self.model_terrain_version}; expected "
                f"{SUPPORTED_MODEL_TERRAIN_VERSION}"
            )
        effective_time = datetime.fromisoformat(
            SUPPORTED_MODEL_TERRAIN_VERSION
        ).replace(tzinfo=UTC)
        if self.initial_time_utc < effective_time:
            raise TerrainValidationError(
                "Pzs initial_time_utc predates the supported terrain update"
            )
        if self.source_kind != "official":
            raise TerrainValidationError("source.kind must be official")
        if "JMBSC" not in self.source_provider.upper():
            raise TerrainValidationError(
                "source.provider must identify the official JMBSC source"
            )
        parsed_url = urlparse(self.source_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise TerrainValidationError("source.url must be an HTTP(S) URL")
        if self.private_repository_bundling not in {"permitted", "prohibited"}:
            raise TerrainValidationError(
                "terms.private_repository_bundling must be permitted or prohibited"
            )
        required_references = {
            TECHNICAL_REFERENCE_619,
            TECHNICAL_REFERENCE_648,
        }
        if not required_references.issubset(self.technical_references):
            raise TerrainValidationError(
                "technical_references must include JMA technical information "
                "No. 619 and No. 648"
            )

    def verify_file(self, path: str | Path) -> str:
        artifact = Path(path)
        if artifact.name != self.file_name:
            raise TerrainValidationError(
                f"source filename mismatch: expected {self.file_name}, "
                f"got {artifact.name}"
            )
        try:
            digest = sha256_file(artifact)
        except OSError as exc:
            raise TerrainValidationError(
                f"cannot read terrain source {artifact}: {exc}"
            ) from exc
        if digest != self.sha256:
            raise TerrainValidationError(
                f"terrain source SHA-256 mismatch: expected {self.sha256}, "
                f"got {digest}"
            )
        return digest

    def cache_metadata(self, manifest_sha256: str) -> dict:
        return {
            "source_kind": self.source_kind,
            "source": self.source_provider,
            "source_url": self.source_url,
            "source_file_name": self.file_name,
            "source_sha256": self.sha256,
            "source_initial_time_utc": _utc_text(self.initial_time_utc),
            "model_terrain_version": self.model_terrain_version,
            "source_manifest_sha256": manifest_sha256,
            "source_acquired_at_utc": _utc_text(self.acquired_at_utc),
            "private_repository_bundling": self.private_repository_bundling,
            "usage_terms": self.usage_terms,
            "redistribution_terms": self.redistribution_terms,
            "terms_reference": self.terms_reference,
            "technical_references": list(self.technical_references),
        }


def _message_value(message, *names):
    for name in names:
        try:
            value = getattr(message, name)
        except (AttributeError, KeyError, RuntimeError):
            continue
        if value is not None:
            return value
    return None


def _message_initial_time(message) -> datetime | None:
    value = _message_value(message, "analDate")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    data_date = _message_value(message, "dataDate")
    data_time = _message_value(message, "dataTime")
    if data_date is None or data_time is None:
        return None
    try:
        return datetime.strptime(
            f"{int(data_date):08d}{int(data_time):04d}", "%Y%m%d%H%M"
        ).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def validate_pzs_message(message, expected_initial_time: datetime) -> None:
    if int(_message_value(message, "edition") or -1) != 2:
        raise TerrainValidationError("terrain source must use GRIB edition 2")
    discipline = _message_value(message, "discipline")
    if discipline is None or int(discipline) != 0:
        raise TerrainValidationError(
            "terrain source must use meteorological discipline 0"
        )
    parameter = (
        _message_value(message, "parameterCategory"),
        _message_value(message, "parameterNumber"),
    )
    if parameter != (3, 33):
        raise TerrainValidationError(
            "terrain source is not Pzs: expected parameter category/number "
            f"3/33, got {parameter[0]}/{parameter[1]}"
        )
    template = _message_value(message, "gridDefinitionTemplateNumber")
    grid_type = str(
        _message_value(message, "gridType", "typeOfGrid") or ""
    ).lower()
    if int(template or -1) != 30 or "lambert" not in grid_type:
        raise TerrainValidationError(
            "terrain source must use Lambert grid definition template 3.30"
        )
    nx = _message_value(message, "Nx", "Ni")
    ny = _message_value(message, "Ny", "Nj")
    if (int(nx or -1), int(ny or -1)) != (PZS_GRID_NX, PZS_GRID_NY):
        raise TerrainValidationError(
            "terrain source grid must be 817x661; "
            f"got {nx}x{ny}"
        )
    points = _message_value(message, "numberOfPoints", "numberOfDataPoints")
    if points is not None and int(points) != PZS_GRID_POINTS:
        raise TerrainValidationError(
            f"terrain source grid must contain {PZS_GRID_POINTS} points"
        )
    forecast_time = _message_value(message, "forecastTime")
    if forecast_time is None or int(forecast_time) != 0:
        raise TerrainValidationError("terrain source must be FH00")
    product_template = _message_value(
        message, "productDefinitionTemplateNumber"
    )
    if product_template is None or int(product_template) != 0:
        raise TerrainValidationError(
            "terrain source must use product definition template 4.0"
        )
    first_surface = _message_value(message, "typeOfFirstFixedSurface")
    if first_surface is None or int(first_surface) != 1:
        raise TerrainValidationError(
            "terrain source must describe the ground or water surface"
        )
    actual_initial_time = _message_initial_time(message)
    if actual_initial_time is None:
        raise TerrainValidationError(
            "terrain source GRIB does not expose an initial time"
        )
    if actual_initial_time != expected_initial_time.astimezone(UTC):
        raise TerrainValidationError(
            "terrain source GRIB initial time does not match the manifest"
        )


def _lambert_subset(message, bounds: Bounds):
    values = np.asarray(
        np.ma.filled(_message_value(message, "values"), np.nan),
        dtype=float,
    )
    try:
        latitudes, longitudes = message.latlons()
    except Exception as exc:
        raise TerrainValidationError(
            f"cannot calculate Pzs Lambert coordinates: {exc}"
        ) from exc
    latitudes = np.asarray(latitudes, dtype=float)
    longitudes = np.asarray(longitudes, dtype=float)
    expected_shape = (PZS_GRID_NY, PZS_GRID_NX)
    if (
        values.shape != expected_shape
        or latitudes.shape != expected_shape
        or longitudes.shape != expected_shape
    ):
        raise TerrainValidationError(
            "decoded Pzs arrays must all have shape 661x817"
        )
    inside = (
        (latitudes >= bounds.lat_min)
        & (latitudes <= bounds.lat_max)
        & (longitudes >= bounds.lon_min)
        & (longitudes <= bounds.lon_max)
    )
    rows = np.flatnonzero(np.any(inside, axis=1))
    columns = np.flatnonzero(np.any(inside, axis=0))
    if not rows.size or not columns.size:
        raise TerrainValidationError(
            "Pzs Lambert grid does not cover the requested bounds"
        )
    row_start = max(int(rows[0]) - 1, 0)
    row_stop = min(int(rows[-1]) + 2, PZS_GRID_NY)
    column_start = max(int(columns[0]) - 1, 0)
    column_stop = min(int(columns[-1]) + 2, PZS_GRID_NX)
    subset = np.s_[row_start:row_stop, column_start:column_stop]
    result = values[subset], latitudes[subset], longitudes[subset]
    if result[0].shape[0] < 2 or result[0].shape[1] < 2:
        raise TerrainValidationError(
            "requested bounds do not contain an interpolatable Pzs grid"
        )
    if not all(np.isfinite(array).all() for array in result):
        raise TerrainValidationError(
            "Pzs subset contains a missing or non-finite value"
        )
    return result


def _inverse_bilinear(coordinates: np.ndarray):
    p00 = coordinates[0, 0]
    p01 = coordinates[0, 1]
    p10 = coordinates[1, 0]
    p11 = coordinates[1, 1]
    cross = p11 - p10 - p01 + p00
    u = 0.5
    v = 0.5
    for _ in range(12):
        point = p00 + (p01 - p00) * u + (p10 - p00) * v + cross * u * v
        if np.linalg.norm(point, ord=np.inf) < 1e-10:
            break
        derivative_u = p01 - p00 + cross * v
        derivative_v = p10 - p00 + cross * u
        jacobian = np.column_stack((derivative_u, derivative_v))
        determinant = float(np.linalg.det(jacobian))
        if abs(determinant) < 1e-14:
            return None
        delta = np.linalg.solve(jacobian, -point)
        u += float(delta[0])
        v += float(delta[1])
        if not np.isfinite((u, v)).all() or max(abs(u), abs(v)) > 4:
            return None
    residual = p00 + (p01 - p00) * u + (p10 - p00) * v + cross * u * v
    if (
        np.linalg.norm(residual, ord=np.inf) > 1e-7
        or u < -1e-7
        or u > 1 + 1e-7
        or v < -1e-7
        or v > 1 + 1e-7
    ):
        return None
    return min(max(u, 0.0), 1.0), min(max(v, 0.0), 1.0)


@dataclass
class GridTerrainProvider:
    values_m: np.ndarray
    latitudes: np.ndarray
    longitudes: np.ndarray
    source: str = "JMA MSM model terrain (Pzs)"
    source_sha256: str | None = None
    source_kind: str | None = None
    source_url: str | None = None
    source_file_name: str | None = None
    source_initial_time_utc: str | None = None
    model_terrain_version: str | None = None
    source_manifest_sha256: str | None = None
    source_acquired_at_utc: str | None = None
    private_repository_bundling: str | None = None
    usage_terms: str | None = None
    redistribution_terms: str | None = None
    terms_reference: str | None = None
    technical_references: tuple[str, ...] = ()
    cache_schema_version: int = TERRAIN_CACHE_SCHEMA_VERSION

    @classmethod
    def from_grib(
        cls,
        path: str | Path,
        bounds: Bounds,
        source_manifest: str | Path,
    ) -> GridTerrainProvider:
        artifact_path = Path(path)
        manifest_path = Path(source_manifest)
        manifest = TerrainSourceManifest.load(manifest_path)
        manifest.verify_file(artifact_path)
        try:
            validate_grib(artifact_path)
        except (OSError, ValueError) as exc:
            raise TerrainValidationError(
                f"invalid terrain source GRIB: {exc}"
            ) from exc
        try:
            import pygrib
        except ImportError as exc:
            raise TerrainValidationError(
                "pygrib is required to prepare the Pzs terrain cache"
            ) from exc
        try:
            grib = pygrib.open(str(artifact_path))
        except Exception as exc:
            raise TerrainValidationError(
                f"cannot open terrain source GRIB {artifact_path}: {exc}"
            ) from exc
        try:
            iterator = iter(grib)
            try:
                message = next(iterator)
            except StopIteration as exc:
                raise TerrainValidationError(
                    "terrain source GRIB contains no messages"
                ) from exc
            try:
                next(iterator)
            except StopIteration:
                pass
            else:
                raise TerrainValidationError(
                    "Pzs FH00 source must contain exactly one GRIB message"
                )
            validate_pzs_message(message, manifest.initial_time_utc)
            values, latitudes, longitudes = _lambert_subset(message, bounds)
        finally:
            grib.close()
        metadata = manifest.cache_metadata(sha256_file(manifest_path))
        return cls(
            values,
            latitudes,
            longitudes,
            source=metadata["source"],
            source_sha256=metadata["source_sha256"],
            source_kind=metadata["source_kind"],
            source_url=metadata["source_url"],
            source_file_name=metadata["source_file_name"],
            source_initial_time_utc=metadata["source_initial_time_utc"],
            model_terrain_version=metadata["model_terrain_version"],
            source_manifest_sha256=metadata["source_manifest_sha256"],
            source_acquired_at_utc=metadata["source_acquired_at_utc"],
            private_repository_bundling=metadata[
                "private_repository_bundling"
            ],
            usage_terms=metadata["usage_terms"],
            redistribution_terms=metadata["redistribution_terms"],
            terms_reference=metadata["terms_reference"],
            technical_references=tuple(metadata["technical_references"]),
        )

    def _validate_arrays(self) -> None:
        shapes = {
            np.asarray(self.values_m).shape,
            np.asarray(self.latitudes).shape,
            np.asarray(self.longitudes).shape,
        }
        if len(shapes) != 1:
            raise TerrainValidationError(
                "terrain values and coordinates must have identical shapes"
            )
        shape = next(iter(shapes))
        if len(shape) != 2 or shape[0] < 2 or shape[1] < 2:
            raise TerrainValidationError(
                "terrain cache must contain a two-dimensional grid"
            )

    def _metadata_v2(self) -> dict:
        self._validate_arrays()
        required = {
            "source_kind": self.source_kind,
            "source": self.source,
            "source_url": self.source_url,
            "source_file_name": self.source_file_name,
            "source_sha256": self.source_sha256,
            "source_initial_time_utc": self.source_initial_time_utc,
            "model_terrain_version": self.model_terrain_version,
            "source_manifest_sha256": self.source_manifest_sha256,
            "source_acquired_at_utc": self.source_acquired_at_utc,
            "private_repository_bundling": self.private_repository_bundling,
            "usage_terms": self.usage_terms,
            "redistribution_terms": self.redistribution_terms,
            "terms_reference": self.terms_reference,
        }
        missing = sorted(key for key, value in required.items() if not value)
        if missing:
            raise TerrainValidationError(
                "terrain cache v2 provenance is incomplete: "
                + ", ".join(missing)
            )
        if self.source_kind != "official":
            raise TerrainValidationError(
                "terrain cache v2 requires an official source"
            )
        for field, digest in (
            ("source_sha256", self.source_sha256),
            ("source_manifest_sha256", self.source_manifest_sha256),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", str(digest)) is None:
                raise TerrainValidationError(
                    f"terrain cache v2 {field} is not a SHA-256 digest"
                )
        if self.model_terrain_version != SUPPORTED_MODEL_TERRAIN_VERSION:
            raise TerrainValidationError(
                "terrain cache model version is unsupported"
            )
        if not self.technical_references:
            raise TerrainValidationError(
                "terrain cache v2 requires technical references"
            )
        return {
            "schema_version": TERRAIN_CACHE_SCHEMA_VERSION,
            **required,
            "technical_references": list(self.technical_references),
            "source_grid": {
                "projection": "lambert_conformal",
                "nx": PZS_GRID_NX,
                "ny": PZS_GRID_NY,
            },
            "interpolation_method": "native-lambert-bilinear",
        }

    def save(self, path: str | Path) -> Path:
        metadata = self._metadata_v2()
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open("wb") as handle:
            np.savez_compressed(
                handle,
                values_m=self.values_m,
                latitudes=self.latitudes,
                longitudes=self.longitudes,
                metadata=json.dumps(metadata, sort_keys=True),
            )
        temporary.replace(destination)
        sidecar = destination.with_suffix(destination.suffix + ".json")
        sidecar_temp = sidecar.with_suffix(sidecar.suffix + ".tmp")
        sidecar_temp.write_text(
            json.dumps(
                {
                    **metadata,
                    "terrain_cache": destination.name,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        sidecar_temp.replace(sidecar)
        return destination

    @classmethod
    def load(cls, path: str | Path) -> GridTerrainProvider:
        try:
            with np.load(path) as data:
                raw_metadata = data["metadata"].item()
                if isinstance(raw_metadata, bytes):
                    raw_metadata = raw_metadata.decode("utf-8")
                metadata = json.loads(str(raw_metadata))
                values = np.asarray(data["values_m"])
                latitudes = np.asarray(data["latitudes"])
                longitudes = np.asarray(data["longitudes"])
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise TerrainValidationError(
                f"cannot load terrain cache {path}: {exc}"
            ) from exc
        schema_version = int(metadata.get("schema_version", 1))
        if schema_version == 1:
            provider = cls(
                values,
                latitudes,
                longitudes,
                source=metadata["source"],
                source_sha256=metadata.get("source_sha256"),
                cache_schema_version=1,
            )
            provider._validate_arrays()
            return provider
        if schema_version != TERRAIN_CACHE_SCHEMA_VERSION:
            raise TerrainValidationError(
                f"unsupported terrain cache schema_version {schema_version}"
            )
        provider = cls(
            values,
            latitudes,
            longitudes,
            source=metadata.get("source"),
            source_sha256=metadata.get("source_sha256"),
            source_kind=metadata.get("source_kind"),
            source_url=metadata.get("source_url"),
            source_file_name=metadata.get("source_file_name"),
            source_initial_time_utc=metadata.get("source_initial_time_utc"),
            model_terrain_version=metadata.get("model_terrain_version"),
            source_manifest_sha256=metadata.get("source_manifest_sha256"),
            source_acquired_at_utc=metadata.get("source_acquired_at_utc"),
            private_repository_bundling=metadata.get(
                "private_repository_bundling"
            ),
            usage_terms=metadata.get("usage_terms"),
            redistribution_terms=metadata.get("redistribution_terms"),
            terms_reference=metadata.get("terms_reference"),
            technical_references=tuple(
                metadata.get("technical_references", ())
            ),
            cache_schema_version=schema_version,
        )
        provider._metadata_v2()
        return provider

    @property
    def provenance(self) -> dict:
        return {
            "terrain_cache_schema_version": self.cache_schema_version,
            "terrain_source_kind": self.source_kind,
            "terrain_source": self.source,
            "terrain_source_url": self.source_url,
            "terrain_source_file_name": self.source_file_name,
            "terrain_source_sha256": self.source_sha256,
            "terrain_source_initial_time_utc": self.source_initial_time_utc,
            "terrain_model_version": self.model_terrain_version,
            "terrain_source_manifest_sha256": self.source_manifest_sha256,
            "terrain_terms_reference": self.terms_reference,
            "terrain_interpolation_method": (
                "native-lambert-bilinear"
                if self.cache_schema_version == TERRAIN_CACHE_SCHEMA_VERSION
                else "legacy-cache"
            ),
        }

    def __call__(self, latitude: float, longitude: float) -> float | None:
        self._validate_arrays()
        latitude_values = np.asarray(self.latitudes, dtype=float)
        longitude_values = np.asarray(self.longitudes, dtype=float)
        terrain_values = np.asarray(self.values_m, dtype=float)
        longitude_delta = (
            (longitude_values - longitude + 180.0) % 360.0
        ) - 180.0
        longitude_scale = np.cos(np.deg2rad(latitude))
        distances = (
            (latitude_values - latitude) ** 2
            + (longitude_delta * longitude_scale) ** 2
        )
        distances[~np.isfinite(distances)] = np.inf
        if not np.isfinite(distances).any():
            return None
        nearest_row, nearest_column = np.unravel_index(
            int(np.argmin(distances)), distances.shape
        )
        candidates = []
        for row in (nearest_row - 1, nearest_row):
            for column in (nearest_column - 1, nearest_column):
                if (
                    row < 0
                    or column < 0
                    or row + 1 >= terrain_values.shape[0]
                    or column + 1 >= terrain_values.shape[1]
                ):
                    continue
                cell_latitudes = latitude_values[
                    row : row + 2, column : column + 2
                ]
                cell_longitudes = longitude_values[
                    row : row + 2, column : column + 2
                ]
                cell_values = terrain_values[
                    row : row + 2, column : column + 2
                ]
                if not all(
                    np.isfinite(array).all()
                    for array in (
                        cell_latitudes,
                        cell_longitudes,
                        cell_values,
                    )
                ):
                    continue
                coordinates = np.empty((2, 2, 2), dtype=float)
                coordinates[:, :, 0] = cell_latitudes - latitude
                coordinates[:, :, 1] = (
                    ((cell_longitudes - longitude + 180.0) % 360.0) - 180.0
                ) * longitude_scale
                weights = _inverse_bilinear(coordinates)
                if weights is None:
                    continue
                u, v = weights
                value = (
                    cell_values[0, 0] * (1 - u) * (1 - v)
                    + cell_values[0, 1] * u * (1 - v)
                    + cell_values[1, 0] * (1 - u) * v
                    + cell_values[1, 1] * u * v
                )
                residual = float(
                    np.min(
                        (
                            cell_latitudes - latitude
                        )
                        ** 2
                        + (
                            (
                                (
                                    cell_longitudes
                                    - longitude
                                    + 180.0
                                )
                                % 360.0
                            )
                            - 180.0
                        )
                        ** 2
                    )
                )
                candidates.append((residual, float(value)))
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])[1]
