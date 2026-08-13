"""Regression tests for exact fare arithmetic and legacy CSV loading."""

from __future__ import annotations

import unittest
from datetime import date, datetime
from fractions import Fraction
from pathlib import Path

from fare_data import Flight, format_money, load_dataset

REPOSITORY = Path(__file__).parents[1]

def make_flight(fares: dict[int, int]) -> Flight:
    """Build a minimal nonstop Flight for arithmetic tests."""
    return Flight(
        flight_date=date(2026, 11, 29),
        origin="SFO",
        destination="JFK",
        airline="Delta",
        flight="DL 669",
        itinerary_id="dl-669",
        depart_at=datetime(2026, 11, 29, 14, 0),
        arrive_at=datetime(2026, 11, 29, 22, 29),
        aircraft="Boeing 767",
        cabin="Economy",
        stops=0,
        notes="",
        currency="USD",
        segments="",
        fares=fares,
        search_ids={},
        fetched_at={},
    )

class FareArithmeticTests(unittest.TestCase):
    """Exercise exact totals and conservative whole-dollar bounds."""

    def test_dl_669_bound_never_overstates_saving(self) -> None:
        flight = make_flight({1: 829, 2: 1657, 3: 3131})

        self.assertEqual(flight.seat_fare(2), Fraction(1657, 2))
        self.assertEqual(flight.split_bound_total(3), Fraction(16207, 6))
        self.assertEqual(flight.split_ceiling_total(3), 2702)
        self.assertEqual(flight.best_total(3), 2702)
        self.assertEqual(flight.split_saving(3), 429)
        self.assertEqual(flight.best_per_person(3), Fraction(2702, 3))

    def test_missing_sweep_rows_disable_split_bound(self) -> None:
        flight = make_flight({1: 180, 3: 626})

        self.assertIsNone(flight.split_bound_total(3))
        self.assertEqual(flight.best_total(3), 626)
        self.assertFalse(flight.wants_split(3))

    def test_money_format_retains_per_seat_precision(self) -> None:
        self.assertEqual(format_money(Fraction(539, 3), always_cents=True), "$179.67")
        self.assertEqual(format_money(1295), "$1,295")

class LegacyDatasetTests(unittest.TestCase):
    """Keep the bundled pre-v2 CSVs usable after the schema upgrade."""

    def test_bundled_data_loads_with_route_and_party_sizes(self) -> None:
        dataset = load_dataset(
            REPOSITORY / "data" / "jfk_sfo_2026-11-20-2026-11-25_3pax.csv"
        )

        self.assertEqual(dataset.info.origin, "JFK")
        self.assertEqual(dataset.info.destination, "SFO")
        self.assertEqual(len(dataset.flights), 121)
        self.assertTrue(all(set(flight.fares) == {1, 2, 3} for flight in dataset.flights))

if __name__ == "__main__":
    unittest.main()
