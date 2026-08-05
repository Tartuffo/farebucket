"""Fetch one-way flight schedules and fares into CSV via the SerpApi Google Flights endpoint.

Reads the API key from SERPAPI_KEY.txt beside the script, overridable with
--key-file, and falling back to the SERPAPI_KEY environment variable. Writes
to ./data with a name derived from the route and date span. Progress and
warnings go to stderr so stdout stays a clean pipe.

Examples:
    uv run python fetch_flights.py JFK SFO --start 2026-11-24 \\
        --cabin business --max-stops 0 --adults 3 --sweep
    # -> data/jfk_sfo_2026-11-24_3pax.csv

    uv run python fetch_flights.py JFK SFO --start 2026-11-17 --end 2026-11-25 \\
        --cabin business --max-stops 0 --adults 2 --sweep --max-price 1600
    # -> data/jfk_sfo_2026-11-17-2026-11-25_2pax.csv

A sweep at --adults 3 queries party sizes 1, 2, and 3, so one run covers
both a two-person and a three-person trip.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http.client import HTTPException, HTTPResponse
from pathlib import Path
from typing import TextIO, TypedDict, cast

API_URL = "https://serpapi.com/search.json"
ONE_WAY = "2"

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
DEFAULT_KEY_FILE = HERE / "SERPAPI_KEY.txt"
KEY_ENV_VAR = "SERPAPI_KEY"
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
RETRYABLE_STATUS = frozenset({429})

CSV_COLUMNS = (
    "date",
    "airline",
    "flight",
    "depart",
    "arrive",
    "arrives_next_day",
    "aircraft",
    "cabin",
    "price",
    "adults",
    "stops",
    "notes",
)

# Human-facing cabin names mapped to SerpApi travel_class codes.
CABIN_CODES = {
    "economy": "1",
    "coach": "1",
    "premium-economy": "2",
    "business": "3",
    "first": "4",
}

# Our --max-stops (0 == nonstop) mapped to SerpApi's stops codes.
MAX_STOPS_CODES = {0: "1", 1: "2", 2: "3"}

# Extension strings worth surfacing in the notes column.
SEAT_KEYWORDS = ("flat", "suite", "recliner", "legroom", "seat", "pod")


class AirportInfo(TypedDict, total=False):
    name: str
    id: str
    time: str


class LegInfo(TypedDict, total=False):
    departure_airport: AirportInfo
    arrival_airport: AirportInfo
    airplane: str
    airline: str
    travel_class: str
    flight_number: str
    legroom: str
    extensions: list[str]


class Itinerary(TypedDict, total=False):
    flights: list[LegInfo]
    price: int
    total_duration: int


class SerpApiResponse(TypedDict, total=False):
    best_flights: list[Itinerary]
    other_flights: list[Itinerary]
    error: str


class FlightRecord(TypedDict):
    date: str
    airline: str
    flight: str
    depart: str
    arrive: str
    arrives_next_day: str
    aircraft: str
    cabin: str
    price: int
    adults: int
    stops: int
    notes: str


type RecordKey = tuple[str, str, str, int]


def warn(message: str) -> None:
    """Print a progress or warning line to stderr.

    Args:
        message: Text to emit.
    """
    print(message, file=sys.stderr)


def daterange(start: date, end: date) -> list[date]:
    """Build the inclusive list of dates to query.

    Args:
        start: First date.
        end: Last date, inclusive.

    Returns:
        Every date from start through end.

    Raises:
        ValueError: If end precedes start.
    """
    if end < start:
        raise ValueError(f"end date {end} precedes start date {start}")
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def parse_stamp(stamp: str) -> datetime:
    """Parse a SerpApi timestamp such as '2026-11-17 06:00'.

    Args:
        stamp: Timestamp text.

    Returns:
        The parsed datetime.
    """
    return datetime.strptime(stamp.strip(), "%Y-%m-%d %H:%M")


def format_clock(moment: datetime) -> str:
    """Render a datetime as a 12-hour clock string like '6:00 AM'.

    Args:
        moment: The datetime to format.

    Returns:
        Clock text without a leading zero on the hour.
    """
    hour = moment.hour % 12 or 12
    meridiem = "AM" if moment.hour < 12 else "PM"
    return f"{hour}:{moment.minute:02d} {meridiem}"


def extract_notes(leg: LegInfo) -> str:
    """Pull a short seat description out of a leg's extensions.

    Args:
        leg: The flight leg.

    Returns:
        A comma-joined note, or an empty string if nothing relevant is present.
    """
    extensions = leg.get("extensions", [])
    seat_notes = [
        text
        for text in extensions
        if any(word in text.lower() for word in SEAT_KEYWORDS) and "emission" not in text.lower()
    ]
    if seat_notes:
        return "; ".join(seat_notes[:2])
    return leg.get("legroom", "")


def build_query(
    origin: str,
    destination: str,
    day: date,
    cabin_code: str,
    adults: int,
    max_stops: int | None,
    api_key: str,
) -> str:
    """Assemble the SerpApi request URL for one date and cabin.

    Args:
        origin: Departure airport code.
        destination: Arrival airport code.
        day: Outbound date.
        cabin_code: SerpApi travel_class value.
        adults: Passenger count.
        max_stops: Maximum stops, 0 for nonstop, or None for any.
        api_key: SerpApi key.

    Returns:
        The fully encoded request URL.
    """
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": day.isoformat(),
        "type": ONE_WAY,
        "travel_class": cabin_code,
        "adults": str(adults),
        "currency": "USD",
        "hl": "en",
        "gl": "us",
        "api_key": api_key,
    }
    if max_stops is not None:
        params["stops"] = MAX_STOPS_CODES[max_stops]
    return f"{API_URL}?{urllib.parse.urlencode(params)}"


def request_flights(url: str) -> SerpApiResponse:
    """Call SerpApi and decode the JSON body, retrying transient failures.

    Truncated reads and 5xx/429 responses are retried with linear backoff.
    Other 4xx responses are permanent (bad key, bad parameters) and fail fast.

    Args:
        url: The request URL.

    Returns:
        The decoded response, or an empty mapping if every attempt failed.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            opened = cast(
                HTTPResponse, urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS)
            )
            with opened as response:
                raw = response.read()
            return cast(SerpApiResponse, json.loads(raw.decode("utf-8")))
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_STATUS and exc.code < 500:
                warn(f"  HTTP {exc.code} {exc.reason} (not retryable)")
                return {}
            problem = f"HTTP {exc.code}"
        except (HTTPException, TimeoutError, OSError, json.JSONDecodeError) as exc:
            problem = f"{type(exc).__name__}"

        if attempt == MAX_ATTEMPTS:
            warn(f"  gave up after {MAX_ATTEMPTS} attempts ({problem})")
            return {}
        warn(f"  attempt {attempt} failed ({problem}), retrying")
        time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return {}


def itinerary_to_record(itinerary: Itinerary, day: date, adults: int) -> FlightRecord | None:
    """Flatten one itinerary into a CSV record.

    For multi-leg itineraries the first leg's departure and the last leg's
    arrival are used, and the operating detail is taken from the first leg.
    The price Google reports is the total for the whole party, so the record
    carries the passenger count alongside it.

    Args:
        itinerary: A single itinerary from the response.
        day: The queried outbound date.
        adults: Passenger count the search was run for.

    Returns:
        The record, or None if the itinerary is missing required fields.
    """
    legs = itinerary.get("flights", [])
    price = itinerary.get("price")
    if not legs or price is None:
        return None

    first, last = legs[0], legs[-1]
    departure = first.get("departure_airport", {}).get("time")
    arrival = last.get("arrival_airport", {}).get("time")
    if not departure or not arrival:
        return None

    departed_at = parse_stamp(departure)
    arrived_at = parse_stamp(arrival)
    return FlightRecord(
        date=day.isoformat(),
        airline=first.get("airline", ""),
        flight=first.get("flight_number", ""),
        depart=format_clock(departed_at),
        arrive=format_clock(arrived_at),
        arrives_next_day=str(arrived_at.date() > departed_at.date()).lower(),
        aircraft=first.get("airplane", ""),
        cabin=first.get("travel_class", ""),
        price=int(price),
        adults=adults,
        stops=len(legs) - 1,
        notes=extract_notes(first),
    )


def collect_records(
    response: SerpApiResponse, day: date, max_price: int | None, adults: int = 1
) -> list[FlightRecord]:
    """Turn one API response into filtered CSV records.

    The max_price bound is per person, so it is compared against the party
    total divided by the passenger count.

    Args:
        response: The decoded SerpApi response.
        day: The queried outbound date.
        max_price: Per-person upper price bound, or None for no cap.
        adults: Passenger count the search was run for.

    Returns:
        Records that pass the price filter.
    """
    itineraries = [*response.get("best_flights", []), *response.get("other_flights", [])]
    records: list[FlightRecord] = []
    for itinerary in itineraries:
        record = itinerary_to_record(itinerary, day, adults)
        if record is None:
            continue
        if max_price is not None and round(record["price"] / adults) > max_price:
            continue
        records.append(record)
    return records


def dedupe(records: list[FlightRecord]) -> list[FlightRecord]:
    """Drop duplicate flights, keeping the cheapest fare for each.

    The same flight often appears in both best_flights and other_flights,
    and across cabin passes. Party size is part of the key so a passenger
    sweep keeps one row per flight per party size.

    Args:
        records: Records in collection order.

    Returns:
        Unique records sorted by date, departure time, then party size.
    """
    cheapest: dict[RecordKey, FlightRecord] = {}
    for record in records:
        key: RecordKey = (record["date"], record["flight"], record["depart"], record["adults"])
        existing = cheapest.get(key)
        if existing is None or record["price"] < existing["price"]:
            cheapest[key] = record
    return sorted(
        cheapest.values(),
        key=lambda r: (r["date"], _clock_offset(r["depart"]), r["adults"]),
    )


def _clock_offset(clock: str) -> timedelta:
    """Convert a 12-hour clock string into an offset from midnight.

    Args:
        clock: Text such as '6:00 AM'.

    Returns:
        The time past midnight.
    """
    moment = datetime.strptime(clock, "%I:%M %p")
    return timedelta(hours=moment.hour, minutes=moment.minute)


def load_api_key(key_file: Path) -> str:
    """Read the SerpApi key from a file, falling back to the environment.

    Args:
        key_file: Path to a file containing just the key.

    Returns:
        The key, or an empty string if no source has one.
    """
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip()
        if key:
            return key
        warn(f"{key_file.name} is empty, falling back to ${KEY_ENV_VAR}")
    return os.environ.get(KEY_ENV_VAR, "").strip()


def default_output_path(
    origin: str, destination: str, days: list[date], adults: int
) -> Path:
    """Derive the CSV path from the route, date span, and party size.

    Single-day pulls get one date in the name; spans get both. Party size is
    always included so runs at different passenger counts never collide.

    Args:
        origin: Departure airport code.
        destination: Arrival airport code.
        days: The dates being queried, in order.
        adults: Largest party size queried.

    Returns:
        Path under the data directory.
    """
    stem = f"{origin.lower()}_{destination.lower()}_{days[0].isoformat()}"
    if len(days) > 1:
        stem = f"{stem}-{days[-1].isoformat()}"
    return DATA_DIR / f"{stem}_{adults}pax.csv"


def write_csv(records: list[FlightRecord], stream: TextIO) -> None:
    """Write records as CSV with the standard header.

    Args:
        records: Records to write.
        stream: Destination stream.
    """
    writer: csv.DictWriter[str] = csv.DictWriter(
        stream, fieldnames=list(CSV_COLUMNS), extrasaction="ignore"
    )
    writer.writeheader()
    writer.writerows(records)


def build_parser() -> argparse.ArgumentParser:
    """Define the command line interface.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        description="Fetch one-way flights into CSV via SerpApi's Google Flights endpoint.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _ = parser.add_argument("origin", help="departure airport code, e.g. JFK")
    _ = parser.add_argument("destination", help="arrival airport code, e.g. SFO")
    _ = parser.add_argument(
        "--start", required=True, help="first outbound date, YYYY-MM-DD"
    )
    _ = parser.add_argument(
        "--end", help="last outbound date, YYYY-MM-DD (defaults to --start)"
    )
    _ = parser.add_argument(
        "--cabin",
        nargs="+",
        choices=sorted(CABIN_CODES),
        default=["economy"],
        help="one or more cabins to query (default: economy)",
    )
    _ = parser.add_argument(
        "--max-price",
        type=int,
        help="drop fares above this amount PER PERSON (Google prices the whole party)",
    )
    _ = parser.add_argument(
        "--max-stops",
        type=int,
        choices=sorted(MAX_STOPS_CODES),
        help="0 for nonstop only; omit for any number of stops",
    )
    _ = parser.add_argument("--adults", type=int, default=1, help="passenger count (default: 1)")
    _ = parser.add_argument(
        "--sweep",
        action="store_true",
        help=(
            "query every party size from 1 to --adults, revealing shallow fare "
            + "buckets where booking seats separately beats booking them together"
        ),
    )
    _ = parser.add_argument(
        "--key-file",
        default=str(DEFAULT_KEY_FILE),
        help=f"file holding the SerpApi key (default: {DEFAULT_KEY_FILE.name})",
    )
    _ = parser.add_argument(
        "--output",
        help="CSV path to write; '-' for stdout (default: auto-named under data/)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    origin = cast(str, args.origin).upper()
    destination = cast(str, args.destination).upper()
    cabins = cast(list[str], args.cabin)
    max_price = cast(int | None, args.max_price)
    max_stops = cast(int | None, args.max_stops)
    adults = cast(int, args.adults)
    sweep = cast(bool, args.sweep)
    output = cast(str | None, args.output)
    party_sizes = list(range(1, adults + 1)) if sweep else [adults]

    key_file = Path(cast(str, args.key_file))
    api_key = load_api_key(key_file)
    if not api_key:
        warn(f"No API key found in {key_file} or ${KEY_ENV_VAR}.")
        return 1

    end_arg = cast(str | None, args.end)
    try:
        start = date.fromisoformat(cast(str, args.start))
        end = date.fromisoformat(end_arg) if end_arg else start
        days = daterange(start, end)
    except ValueError as exc:
        warn(f"bad date range: {exc}")
        return 1

    # One request per date per cabin; cabins are separate Google Flights searches.
    codes = sorted({CABIN_CODES[name] for name in cabins})
    total_requests = len(days) * len(codes) * len(party_sizes)
    plan = f"{len(days)} dates x {len(codes)} cabins x {len(party_sizes)} party sizes"
    warn(f"{origin} -> {destination}: {plan} = {total_requests} requests")

    # A sweep exists to compare party sizes, so filtering here would discard the
    # expensive large-party row that the split-booking math depends on.
    fetch_cap = max_price
    if sweep and max_price is not None:
        warn(f"  --sweep: ${max_price:,} cap deferred to analysis to keep party pricing intact")
        fetch_cap = None

    records: list[FlightRecord] = []
    for day in days:
        for code in codes:
            for party in party_sizes:
                url = build_query(origin, destination, day, code, party, max_stops, api_key)
                response = request_flights(url)
                error = response.get("error")
                if error:
                    warn(f"  {day} cabin {code} party {party}: {error}")
                    continue
                found = collect_records(response, day, fetch_cap, party)
                records.extend(found)
                warn(f"  {day} cabin {code} party {party}: {len(found)} flights")
                time.sleep(REQUEST_DELAY_SECONDS)

    unique = dedupe(records)
    warn(f"{len(unique)} unique flights after dedupe")

    if output == "-":
        write_csv(unique, sys.stdout)
        return 0

    destination_path = (
        Path(output) if output else default_output_path(origin, destination, days, adults)
    )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with destination_path.open("w", newline="", encoding="utf-8") as handle:
        write_csv(unique, handle)
    warn(f"wrote {destination_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
