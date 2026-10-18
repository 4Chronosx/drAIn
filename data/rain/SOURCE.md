# Mandaue hourly rain, 2011–2025

`openmeteo_mandaue_hourly_2011_2025.csv`: hourly precipitation (mm) at 10.33° N, 123.94° E, 2011-01-01 00:00 to 2025-12-31 23:00, local time (Asia/Manila). 131,496 rows.

- **Source:** Open-Meteo Historical Weather API (`archive-api.open-meteo.com/v1/archive`), `hourly=precipitation`, `models=era5`. Data licence: CC BY 4.0 (Open-Meteo; ERA5 from the Copernicus Climate Change Service, ECMWF).
- **Dataset:** ERA5 reanalysis, about 0.25° (roughly 25 km) cells, hourly. It smooths short bursts, so short-duration maxima (1 h) understate what a rain gauge would record.
- **Fetched:** 2026-10-18, by `drain.rain_history.fetch_hourly` (a year per request). Refetch with `python -m scripts.flood_rain_thresholds --refresh-rain`.
- **Convention:** each value is the rain of the hour ending at its timestamp, so the value stamped 00:00 belongs to the previous day.
