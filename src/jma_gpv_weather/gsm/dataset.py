"""GSM query boundary using the shared weather dataset and interpolators."""
from dataclasses import replace
import math

from ..errors import GsmProcessingError
from ..models import (AloftQuery, AloftTemperatureQuery, SurfaceTemperatureQuery,
                      Availability, WeatherResult)
from ..weather import WeatherDataset
from .spec import LEVELS_HPA, required_valid_times


class PreparedGsmForecast(WeatherDataset):
    trace_pressure_levels = True
    def __init__(self, selection, surface, pressure, source_hashes, requirements):
        super().__init__(selection, surface, pressure, source_hashes, pressure_levels=LEVELS_HPA)
        self.requirements = requirements

    def _provenance(self, method, trace=None):
        result = super()._provenance(method, trace)
        return replace(result, trace={**result.trace, 'model': 'GSM_JAPAN',
                                     'pressure_levels_hpa': LEVELS_HPA})

    def query(self, query):
        if not isinstance(query, (AloftQuery, SurfaceTemperatureQuery)):
            raise ValueError(f'Unsupported GSM query: {type(query).__name__}')
        if query.valid_time.tzinfo is None:
            raise ValueError('valid_time must be timezone-aware')
        coords = [query.latitude, query.longitude]
        if isinstance(query, AloftQuery):
            coords.append(query.altitude_msl_m)
        if not all(math.isfinite(v) for v in coords):
            raise ValueError('query coordinates and altitude must be finite')
        # Restrict temporal interpolation to prepared official brackets. Missing
        # intermediate data must never silently become a wider interpolation.
        from ..models import ForecastRequirements, WeatherVariable
        variables = ({WeatherVariable.SURFACE_TEMPERATURE} if isinstance(query, SurfaceTemperatureQuery)
                     else {WeatherVariable.ALOFT_TEMPERATURE} if isinstance(query, AloftTemperatureQuery)
                     else {WeatherVariable.ALOFT_WIND, WeatherVariable.ALOFT_TEMPERATURE})
        requested = ForecastRequirements((query.valid_time,), frozenset(variables))
        needed = required_valid_times(requested, self.selection.run_utc)
        prepared = required_valid_times(self.requirements, self.selection.run_utc)
        if needed is None or any(not set(ts) <= set(prepared.get(kind, ())) for kind, ts in needed.items()):
            return WeatherResult(Availability.UNAVAILABLE, 'weather', reason_code='TIME_NOT_PREPARED',
                                 provenance=self._provenance('none'))
        try:
            if isinstance(query, SurfaceTemperatureQuery):
                return super().query_surface_temperature(query)
            if isinstance(query, AloftTemperatureQuery):
                return super().query_aloft_temperature(query)
            return super().query_aloft(query)
        except (ValueError, IndexError, KeyError) as exc:
            raise GsmProcessingError(f'GSM interpolation failed: {exc}') from exc

    def query_many(self, queries):
        return tuple(self.query(q) for q in queries)

    def query_surface_temperature(self, query):
        return self.query(query)

    def query_aloft_temperature(self, query):
        return self.query(AloftTemperatureQuery(
            query.latitude, query.longitude, query.valid_time, query.altitude_msl_m
        ))

    def query_aloft(self, query):
        return self.query(query)
