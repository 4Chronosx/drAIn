# Mandaue flood records (Groundsource subset)

`groundsource_mandaue.csv`: 357 flood records inside Mandaue City, 2011–2025.

- **Source:** Google Groundsource, https://doi.org/10.5281/zenodo.18647054. Licence: CC BY 4.0.
- **How the subset was made (2026-10-16):** the full dataset was filtered to records whose polygon centroid falls inside drAIn's Mandaue barangay polygons (`data/mandaue_population.geojson`); `barangay_of_centroid` is the barangay containing that centroid, `(none)` where it fell in a gap between polygons.
- **Generator:** `mandaue_brgy.py`, kept beside the research copy in the Project Drain folder (`docs/research/data/groundsource-mandaue/`), not here. It needs `pyarrow`, `shapely` and a 667 MB download, which the backend shouldn't depend on.
- **Columns:** `start_date, end_date, area_km2, barangay_of_centroid`.

## Known limits

- No source URL, depth, time of day or cause per record.
- Follow-up stories can repeat one flood on later days (`drain/flood_history.py` merges records within 3 days).
- Polygons of 10 km² or more (58 records, about 30 km² each) cover most of the city and can't be pinned to a barangay; they are left out.
- These are **reported** floods, not all floods: coverage follows where people and newsrooms are.
