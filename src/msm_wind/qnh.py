from __future__ import annotations

import math
from dataclasses import dataclass

G0 = 9.80665
RD = 287.05287
ISA_T0_K = 288.15
ISA_LAPSE_K_M = 0.0065


@dataclass(frozen=True)
class QnhEstimate:
    qnh_pa: float
    station_pressure_pa: float
    virtual_temperature_k: float
    terrain_difference_m: float
    method_version: str = "hypsometric-isa-v1"


def saturation_vapor_pressure_pa(temperature_k: float) -> float:
    temperature_c = temperature_k - 273.15
    return 611.21 * math.exp(
        (18.678 - temperature_c / 234.5) * temperature_c / (257.14 + temperature_c)
    )


def virtual_temperature_k(
    temperature_k: float, relative_humidity_percent: float, pressure_pa: float
) -> float:
    if not 0 <= relative_humidity_percent <= 100:
        raise ValueError("relative humidity must be between 0 and 100 percent")
    vapor_pressure = relative_humidity_percent / 100 * saturation_vapor_pressure_pa(temperature_k)
    if vapor_pressure >= pressure_pa:
        raise ValueError("vapor pressure must be below total pressure")
    mixing_ratio = 0.62198 * vapor_pressure / (pressure_pa - vapor_pressure)
    specific_humidity = mixing_ratio / (1 + mixing_ratio)
    return temperature_k * (1 + 0.608 * specific_humidity)


def estimate_qnh(
    surface_pressure_pa: float,
    surface_temperature_k: float,
    relative_humidity_percent: float,
    model_terrain_m: float,
    site_elevation_m: float,
) -> QnhEstimate:
    if surface_pressure_pa <= 0 or surface_temperature_k <= 0:
        raise ValueError("pressure and temperature must be positive")
    delta_z = site_elevation_m - model_terrain_m
    tv = virtual_temperature_k(
        surface_temperature_k, relative_humidity_percent, surface_pressure_pa
    )
    mean_tv = tv - 0.5 * ISA_LAPSE_K_M * delta_z
    if mean_tv <= 0:
        raise ValueError("invalid mean virtual temperature")
    station_pressure = surface_pressure_pa * math.exp(-G0 * delta_z / (RD * mean_tv))
    isa_factor = 1 - ISA_LAPSE_K_M * site_elevation_m / ISA_T0_K
    if isa_factor <= 0:
        raise ValueError("site elevation is outside the ISA troposphere formula")
    exponent = G0 / (RD * ISA_LAPSE_K_M)
    qnh = station_pressure / (isa_factor**exponent)
    return QnhEstimate(qnh, station_pressure, tv, delta_z)
