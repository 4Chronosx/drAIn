"""Tests for the Open-Meteo rain history. No test touches the network."""

from __future__ import annotations

import urllib.parse
from datetime import date, datetime, timedelta

import pytest

from drain.rain_history import daily_features, fetch_hourly, read_cache, write_cache


def series(start: datetime, values: list[float]) -> list[tuple[datetime, float]]:
    return [(start + timedelta(hours=i), mm) for i, mm in enumerate(values)]


def first_hour(day: date) -> datetime:
    """The first stamp belonging to ``day``: 01:00, since 00:00 closes the day before."""
    return datetime(day.year, day.month, day.day, 1)


class TestFeatures:
    def test_rolling_maxima_and_total(self):
        values = [0.0] * 24
        values[5], values[6], values[7] = 4.0, 6.0, 2.0
        values[20] = 5.0
        features = daily_features(series(first_hour(date(2020, 1, 1)), values))
        day = features[date(2020, 1, 1)]
        assert (day.max_1h, day.max_3h, day.max_6h, day.total_24h) == (6.0, 12.0, 12.0, 17.0)

    def test_the_midnight_stamp_belongs_to_the_day_before(self):
        values = [0.0] * 48
        values[23] = 9.0  # stamped 2020-01-02 00:00
        features = daily_features(series(first_hour(date(2020, 1, 1)), values))
        assert features[date(2020, 1, 1)].total_24h == 9.0
        assert features[date(2020, 1, 2)].total_24h == 0.0

    def test_windows_reach_back_into_the_previous_day(self):
        values = [0.0] * 48
        values[23], values[24] = 3.0, 4.0  # last hour of day 1, first of day 2
        features = daily_features(series(first_hour(date(2020, 1, 1)), values))
        assert features[date(2020, 1, 2)].max_3h == 7.0

    def test_antecedent_is_the_three_days_before(self):
        days = [1.0, 2.0, 3.0, 4.0, 0.0]
        values = [mm for total in days for mm in [total] + [0.0] * 23]
        features = daily_features(series(first_hour(date(2020, 1, 1)), values))
        assert features[date(2020, 1, 4)].antecedent_3d == 6.0
        assert features[date(2020, 1, 5)].antecedent_3d == 9.0
        assert features[date(2020, 1, 3)].antecedent_3d is None

    def test_only_full_days_get_features(self):
        features = daily_features(series(datetime(2020, 1, 1, 0), [1.0] * 30))
        assert list(features) == [date(2020, 1, 1)]

    def test_a_gap_is_an_error(self):
        broken = series(first_hour(date(2020, 1, 1)), [0.0] * 30)
        del broken[10]
        with pytest.raises(ValueError, match="not continuous"):
            daily_features(broken)


class TestFetch:
    @staticmethod
    def stub(calls, value=1.0):
        def get_json(url):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            calls.append(query)
            start = date.fromisoformat(query["start_date"][0])
            end = date.fromisoformat(query["end_date"][0])
            hours = ((end - start).days + 1) * 24
            first = datetime(start.year, start.month, start.day)
            stamps = [first + timedelta(hours=i) for i in range(hours)]
            return {
                "hourly": {
                    "time": [s.strftime("%Y-%m-%dT%H:%M") for s in stamps],
                    "precipitation": [value] * hours,
                }
            }

        return get_json

    def test_fetches_a_year_per_request_in_local_time(self):
        calls = []
        hourly = fetch_hourly(date(2019, 12, 30), date(2020, 1, 2), get_json=self.stub(calls))
        assert [(c["start_date"][0], c["end_date"][0]) for c in calls] == [
            ("2019-12-30", "2019-12-31"),
            ("2020-01-01", "2020-01-02"),
        ]
        assert all(c["timezone"] == ["Asia/Manila"] and c["models"] == ["era5"] for c in calls)
        assert len(hourly) == 4 * 24
        assert hourly[0] == (datetime(2019, 12, 30), 1.0)

    def test_a_missing_hour_is_an_error(self):
        with pytest.raises(ValueError, match="no precipitation"):
            fetch_hourly(date(2020, 1, 1), date(2020, 1, 1), get_json=self.stub([], value=None))


def test_the_cache_round_trips(tmp_path):
    hourly = series(datetime(2020, 1, 1), [0.0, 1.5, 12.3])
    path = tmp_path / "rain.csv"
    write_cache(hourly, path)
    assert read_cache(path) == hourly


def test_the_shipped_cache_covers_2011_to_2025():
    features = daily_features(read_cache())
    assert min(features) == date(2011, 1, 1)
    assert max(features) == date(2025, 12, 30)
