"""Regression tests for trip filtering and date-correct summaries."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch

import summarize
from analyze_trips import (
    Criteria,
    TripOption,
    find_trip_options,
    passes_outbound_filters,
    validate_dataset_pair,
)
from fare_data import DatasetInfo, Flight

REPOSITORY = Path(__file__).parents[1]

def make_flight(
    flight_date: date,
    depart_hour: int,
    arrive_at: datetime,
    fares: dict[int, int],
    flight_number: str,
) -> Flight:
    """Build a minimal Flight for analysis tests."""
    return Flight(
        flight_date=flight_date,
        origin="AAA",
        destination="BBB",
        airline="Example",
        flight=flight_number,
        itinerary_id=flight_number,
        depart_at=datetime.combine(flight_date, time(depart_hour, 0)),
        arrive_at=arrive_at,
        aircraft="A320",
        cabin="Economy",
        stops=0,
        notes="",
        currency="USD",
        segments="",
        fares=fares,
        search_ids={},
        fetched_at={},
    )

class FilterTests(unittest.TestCase):
    """Exercise corrected trip constraints."""

    def test_arrival_cutoff_rejects_next_day_flight(self) -> None:
        day = date(2026, 11, 20)
        flight = make_flight(
            day,
            22,
            datetime(2026, 11, 21, 0, 30),
            {1: 100},
            "EA 1",
        )
        criteria = Criteria(
            passengers=1,
            full_days=(1,),
            options=3,
            latest_outbound_arrival=time(23, 59),
        )

        self.assertFalse(passes_outbound_filters(flight, criteria))

    def test_max_price_caps_the_round_trip_not_each_leg(self) -> None:
        out_day = date(2026, 11, 20)
        back_day = date(2026, 11, 22)
        outbound = make_flight(
            out_day,
            8,
            datetime(2026, 11, 20, 12, 0),
            {1: 300},
            "EA 10",
        )
        inbound = make_flight(
            back_day,
            8,
            datetime(2026, 11, 22, 12, 0),
            {1: 300},
            "EA 11",
        )
        criteria = Criteria(passengers=1, full_days=(1,), options=3, max_price=500)

        self.assertEqual(find_trip_options([outbound], [inbound], 1, criteria), [])

    def test_trip_total_uses_observed_totals_without_per_person_roundtrip(self) -> None:
        out_day = date(2026, 11, 20)
        back_day = date(2026, 11, 22)
        outbound = make_flight(
            out_day,
            8,
            datetime(2026, 11, 20, 12, 0),
            {1: 180, 2: 359, 3: 539},
            "EA 20",
        )
        inbound = make_flight(
            back_day,
            8,
            datetime(2026, 11, 22, 12, 0),
            {1: 252, 2: 504, 3: 756},
            "EA 21",
        )
        option = TripOption(outbound, inbound, 1, 3)

        self.assertEqual(option.total, 1295)

    def test_full_days_are_counted_from_overnight_outbound_arrival(self) -> None:
        out_day = date(2026, 11, 20)
        outbound = make_flight(
            out_day,
            22,
            datetime(2026, 11, 21, 6, 0),
            {1: 100},
            "EA 30",
        )
        too_early = make_flight(
            date(2026, 11, 22),
            10,
            datetime(2026, 11, 22, 14, 0),
            {1: 100},
            "EA 31",
        )
        correct_return = make_flight(
            date(2026, 11, 23),
            10,
            datetime(2026, 11, 23, 14, 0),
            {1: 100},
            "EA 32",
        )
        criteria = Criteria(
            passengers=1,
            full_days=(1,),
            options=3,
            must_include=date(2026, 11, 22),
        )

        options = find_trip_options(
            [outbound], [too_early, correct_return], 1, criteria
        )

        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].inbound, correct_return)

    def test_route_validation_rejects_same_direction_files(self) -> None:
        outbound = DatasetInfo(Path("out.csv"), "JFK", "SFO", "USD", True, 2)
        inbound = DatasetInfo(Path("in.csv"), "JFK", "SFO", "USD", True, 2)

        self.assertEqual(
            validate_dataset_pair(outbound, inbound),
            "routes are not reverses: JFK -> SFO and JFK -> SFO",
        )

class SummaryIntegrationTests(unittest.TestCase):
    """Exercise the documented multi-day sample workflow."""

    def test_multiday_csv_gets_one_labeled_table_per_date(self) -> None:
        csv_path = REPOSITORY / "data" / "sfo_jfk_2026-11-27-2026-12-02_3pax.csv"
        output = io.StringIO()
        arguments = ["summarize.py", str(csv_path), "--parties", "1", "2", "3"]

        with patch("sys.argv", arguments), redirect_stdout(output):
            result = summarize.main()

        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertEqual(rendered.count("SFO -> JFK  |"), 6)
        self.assertIn("Fri Nov 27, 2026", rendered)
        self.assertIn("Wed Dec 2, 2026", rendered)

if __name__ == "__main__":
    unittest.main()
