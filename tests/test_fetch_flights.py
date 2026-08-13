"""Regression tests for itinerary identity and exact fetch filtering."""

from __future__ import annotations

import unittest
from contextlib import redirect_stderr
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from fare_data import load_dataset, manifest_path
from fetch_flights import (
    FetchConfig,
    InvalidResponseError,
    Itinerary,
    SearchJob,
    SerpApiResponse,
    SweepResult,
    collect_records,
    decode_response,
    dedupe,
    itinerary_to_record,
    write_csv_atomic,
    write_fetch_result,
)

REPOSITORY = Path(__file__).parents[1]

def connecting_itinerary(connection: str, cabin: str = "Economy") -> Itinerary:
    """Build an itinerary sharing its first leg with other test itineraries."""
    return Itinerary(
        flights=[
            {
                "departure_airport": {"id": "JFK", "time": "2026-11-20 08:00"},
                "arrival_airport": {"id": "ORD", "time": "2026-11-20 09:30"},
                "airline": "Example Air",
                "flight_number": "EA 100",
                "travel_class": cabin,
                "airplane": "A320",
            },
            {
                "departure_airport": {"id": "ORD", "time": "2026-11-20 10:30"},
                "arrival_airport": {"id": connection, "time": "2026-11-20 13:00"},
                "airline": "Example Air",
                "flight_number": "EA 200",
                "travel_class": cabin,
                "airplane": "A320",
            },
        ],
        price=500,
    )

class ItineraryIdentityTests(unittest.TestCase):
    """Ensure comparisons never merge different products."""

    def test_connections_with_same_first_leg_have_different_ids(self) -> None:
        sfo = itinerary_to_record(
            connecting_itinerary("SFO"), "JFK", "SFO", date(2026, 11, 20), 1
        )
        lax = itinerary_to_record(
            connecting_itinerary("LAX"), "JFK", "LAX", date(2026, 11, 20), 1
        )

        self.assertIsNotNone(sfo)
        self.assertIsNotNone(lax)
        if sfo is None or lax is None:
            self.fail("test itineraries should produce records")
        self.assertNotEqual(sfo["itinerary_id"], lax["itinerary_id"])
        self.assertEqual(sfo["flight"], "EA 100 / EA 200")

    def test_cabins_have_different_ids_and_both_survive_dedupe(self) -> None:
        economy = itinerary_to_record(
            connecting_itinerary("SFO", "Economy"),
            "JFK",
            "SFO",
            date(2026, 11, 20),
            1,
        )
        business = itinerary_to_record(
            connecting_itinerary("SFO", "Business"),
            "JFK",
            "SFO",
            date(2026, 11, 20),
            1,
        )

        self.assertIsNotNone(economy)
        self.assertIsNotNone(business)
        if economy is None or business is None:
            self.fail("test itineraries should produce records")
        self.assertEqual(len(dedupe([economy, business])), 2)

    def test_price_cap_compares_party_total_without_rounding(self) -> None:
        itinerary = connecting_itinerary("SFO")
        itinerary["price"] = 1001
        response = SerpApiResponse(best_flights=[itinerary])

        records = collect_records(
            response,
            "JFK",
            "SFO",
            date(2026, 11, 20),
            max_price=500,
            adults=2,
        )

        self.assertEqual(records, [])

    def test_malformed_api_collection_is_rejected_at_boundary(self) -> None:
        with self.assertRaises(InvalidResponseError):
            _ = decode_response({"best_flights": "not an array"})

class CompletenessTests(unittest.TestCase):
    """Ensure failed sweeps do not replace authoritative data."""

    def test_failed_sweep_preserves_existing_output(self) -> None:
        temporary_root = REPOSITORY / "codex-tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=temporary_root) as directory:
            output = Path(directory) / "fares.csv"
            _ = output.write_text("existing data\n", encoding="utf-8")
            job = SearchJob(date(2026, 11, 20), "1", 1)
            config = FetchConfig(
                origin="JFK",
                destination="SFO",
                days=[job.day],
                adults=1,
                party_sizes=[1],
                cabin_codes=["1"],
                jobs=[job],
                max_stops=0,
                max_price=None,
                fresh=False,
                deep_search=False,
                allow_partial=False,
                key_file=Path("unused"),
                output=str(output),
            )
            result = SweepResult([], ["request failed"], 0)

            with redirect_stderr(StringIO()):
                status = write_fetch_result(config, result, [], "2026-11-20T00:00:00Z")

            self.assertEqual(status, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "existing data\n")
            self.assertFalse(manifest_path(output).exists())

    def test_current_csv_schema_round_trips_through_loader(self) -> None:
        temporary_root = REPOSITORY / "codex-tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        one = itinerary_to_record(
            connecting_itinerary("SFO"), "JFK", "SFO", date(2026, 11, 20), 1
        )
        two_itinerary = connecting_itinerary("SFO")
        two_itinerary["price"] = 1000
        two = itinerary_to_record(
            two_itinerary, "JFK", "SFO", date(2026, 11, 20), 2
        )
        self.assertIsNotNone(one)
        self.assertIsNotNone(two)
        if one is None or two is None:
            self.fail("test itineraries should produce records")

        with TemporaryDirectory(dir=temporary_root) as directory:
            output = Path(directory) / "custom-name.csv"
            write_csv_atomic([one, two], output)
            dataset = load_dataset(output)

            self.assertEqual(dataset.info.route, "JFK -> SFO")
            self.assertEqual(len(dataset.flights), 1)
            self.assertEqual(dataset.flights[0].fares, {1: 500, 2: 1000})

if __name__ == "__main__":
    unittest.main()
