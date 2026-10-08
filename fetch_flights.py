"""Fetch one-way flight schedules and party-size fares from SerpApi."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from http.client import HTTPException, HTTPResponse
from pathlib import Path
from typing import TextIO, TypedDict, cast

from fare_data import format_clock, manifest_path

API_URL = "https://serpapi.com/search.json"
ONE_WAY = "2"
ECONOMY_CODE = "1"
SCHEMA_VERSION = 2

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
    "origin",
    "destination",
    "date",
    "airline",
    "flight",
    "itinerary_id",
    "segments",
    "depart_at",
    "arrive_at",
    "depart",
    "arrive",
    "arrives_next_day",
    "aircraft",
    "cabin",
    "currency",
    "price",
    "adults",
    "stops",
    "notes",
    "search_id",
    "fetched_at",
)

CABIN_CODES = {
    "economy": "1",
    "coach": "1",
    "premium-economy": "2",
    "business": "3",
    "first": "4",
}
MAX_STOPS_CODES = {0: "1", 1: "2", 2: "3"}
SEAT_KEYWORDS = ("flat", "suite", "recliner", "legroom", "seat", "pod")

class AirportInfo(TypedDict, total=False):
    """Airport fields used from a SerpApi flight leg."""

    name: str
    id: str
    time: str

class LegInfo(TypedDict, total=False):
    """Flight-leg fields used from a SerpApi itinerary."""

    departure_airport: AirportInfo
    arrival_airport: AirportInfo
    airplane: str
    airline: str
    travel_class: str
    flight_number: str
    legroom: str
    extensions: list[str]

class Itinerary(TypedDict, total=False):
    """Itinerary fields used from a SerpApi response."""

    flights: list[LegInfo]
    price: int
    total_duration: int
    extensions: list[str]

class SearchMetadata(TypedDict, total=False):
    """Search provenance returned by SerpApi."""

    id: str
    status: str
    created_at: str
    processed_at: str

class SerpApiResponse(TypedDict, total=False):
    """Relevant top-level SerpApi response fields."""

    search_metadata: SearchMetadata
    best_flights: list[Itinerary]
    other_flights: list[Itinerary]
    error: str

class FlightRecord(TypedDict):
    """One output row for an itinerary and party-size search."""

    origin: str
    destination: str
    date: str
    airline: str
    flight: str
    itinerary_id: str
    segments: str
    depart_at: str
    arrive_at: str
    depart: str
    arrive: str
    arrives_next_day: str
    aircraft: str
    cabin: str
    currency: str
    price: int
    adults: int
    stops: int
    notes: str
    search_id: str
    fetched_at: str

class SweepManifest(TypedDict):
    """Sidecar metadata describing a completed or partial sweep."""

    schema_version: int
    complete: bool
    origin: str
    destination: str
    currency: str
    start: str
    end: str
    cabin_codes: list[str]
    max_stops: int | None
    party_sizes: list[int]
    searches_expected: int
    searches_succeeded: int
    failures: list[str]
    fresh: bool
    deep_search: bool
    exclude_basic: bool
    started_at: str
    finished_at: str

@dataclass(frozen=True)
class RequestOutcome:
    """Success or failure of one HTTP search."""

    response: SerpApiResponse | None
    problem: str | None
    search_id: str
    fetched_at: str

@dataclass(frozen=True)
class SearchJob:
    """One date, cabin, and party-size query."""

    day: date
    cabin_code: str
    party: int

@dataclass(frozen=True)
class SweepResult:
    """Records and query-level status from a sweep."""

    records: list[FlightRecord]
    failures: list[str]
    searches_succeeded: int

@dataclass(frozen=True)
class FetchConfig:
    """Validated settings for one fetch run."""

    origin: str
    destination: str
    days: list[date]
    adults: int
    party_sizes: list[int]
    cabin_codes: list[str]
    jobs: list[SearchJob]
    max_stops: int | None
    max_price: int | None
    fresh: bool
    deep_search: bool
    exclude_basic: bool
    allow_partial: bool
    key_file: Path
    output: str | None

class InvalidResponseError(ValueError):
    """Raised when a successful HTTP response is not usable SerpApi JSON."""

type RecordKey = tuple[str, int]

def warn(message: str) -> None:
    """Print progress or warning text to stderr.

    Args:
        message: Text to emit.
    """
    print(message, file=sys.stderr)

def positive_int(raw: str) -> int:
    """Parse a positive command-line integer.

    Args:
        raw: Argument text.

    Returns:
        A positive integer.

    Raises:
        argparse.ArgumentTypeError: If the value is invalid.
    """
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return value

def iso_date(raw: str) -> date:
    """Parse a YYYY-MM-DD command-line date.

    Args:
        raw: Argument text.

    Returns:
        Parsed date.

    Raises:
        argparse.ArgumentTypeError: If the value is invalid.
    """
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {raw!r}") from exc

def airport_code(raw: str) -> str:
    """Parse a three-letter IATA airport code.

    Args:
        raw: Argument text.

    Returns:
        Uppercase airport code.

    Raises:
        argparse.ArgumentTypeError: If the value is not three ASCII letters.
    """
    normalized = raw.upper()
    if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
        raise argparse.ArgumentTypeError(f"expected a three-letter airport code, got {raw!r}")
    return normalized

def daterange(start: date, end: date) -> list[date]:
    """Build an inclusive list of query dates.

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
    """Parse a SerpApi local timestamp.

    Args:
        stamp: Timestamp such as `2026-11-17 06:00`.

    Returns:
        Parsed naive local datetime.
    """
    return datetime.strptime(stamp.strip(), "%Y-%m-%d %H:%M")

def extract_notes(leg: LegInfo) -> str:
    """Extract a concise seat description from one leg.

    Args:
        leg: SerpApi flight leg.

    Returns:
        Relevant extension text or legroom.
    """
    extensions = leg.get("extensions", [])
    seat_notes = [
        text
        for text in extensions
        if any(word in text.lower() for word in SEAT_KEYWORDS)
        and "emission" not in text.lower()
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
    *,
    fresh: bool = False,
    deep_search: bool = False,
    exclude_basic: bool = False,
) -> str:
    """Assemble one SerpApi request URL.

    Args:
        origin: Departure airport code.
        destination: Arrival airport code.
        day: Outbound date.
        cabin_code: SerpApi travel-class code.
        adults: Passenger count.
        max_stops: Maximum stops, or None for any.
        api_key: SerpApi key.
        fresh: Whether to bypass SerpApi's cache.
        deep_search: Whether to request browser-equivalent results.
        exclude_basic: Whether to drop Basic Economy fares. SerpApi rejects
            the filter outside economy, so other cabins ignore it.

    Returns:
        Fully encoded request URL.
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
    if fresh:
        params["no_cache"] = "true"
    if deep_search:
        params["deep_search"] = "true"
    if exclude_basic and cabin_code == ECONOMY_CODE:
        params["exclude_basic"] = "true"
    return f"{API_URL}?{urllib.parse.urlencode(params)}"

def decode_response(decoded: object) -> SerpApiResponse:
    """Validate the response shapes used by the fetch pipeline.

    Args:
        decoded: Value returned by json.loads.

    Returns:
        Response narrowed to the supported SerpApi structure.

    Raises:
        InvalidResponseError: If a field has an unexpected JSON type.
    """
    if not isinstance(decoded, dict):
        raise InvalidResponseError("JSON root is not an object")
    mapping = cast(dict[str, object], decoded)
    if not any(
        key in mapping for key in ("search_metadata", "best_flights", "other_flights", "error")
    ):
        raise InvalidResponseError("response has no SerpApi result fields")
    metadata = mapping.get("search_metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise InvalidResponseError("search_metadata is not an object")
        metadata_mapping = cast(dict[str, object], metadata)
        for key in ("id", "status", "created_at", "processed_at"):
            value = metadata_mapping.get(key)
            if value is not None and not isinstance(value, str):
                raise InvalidResponseError(f"search_metadata.{key} is not a string")
    error = mapping.get("error")
    if error is not None and not isinstance(error, str):
        raise InvalidResponseError("error is not a string")
    for key in ("best_flights", "other_flights"):
        itineraries = mapping.get(key)
        if itineraries is None:
            continue
        if not isinstance(itineraries, list):
            raise InvalidResponseError(f"{key} is not an array")
        for itinerary in cast(list[object], itineraries):
            _validate_itinerary_shape(itinerary, key)
    return cast(SerpApiResponse, mapping)

def _validate_itinerary_shape(itinerary: object, collection: str) -> None:
    """Validate the nested JSON types consumed from one itinerary."""
    if not isinstance(itinerary, dict):
        raise InvalidResponseError(f"{collection} contains a non-object itinerary")
    mapping = cast(dict[str, object], itinerary)
    price = mapping.get("price")
    if price is not None and (not isinstance(price, int) or isinstance(price, bool)):
        raise InvalidResponseError(f"{collection} itinerary price is not an integer")
    flights = mapping.get("flights")
    if flights is None:
        return
    if not isinstance(flights, list):
        raise InvalidResponseError(f"{collection} itinerary flights is not an array")
    for leg in cast(list[object], flights):
        if not isinstance(leg, dict):
            raise InvalidResponseError(f"{collection} itinerary contains a non-object leg")
        leg_mapping = cast(dict[str, object], leg)
        for text_key in (
            "airplane",
            "airline",
            "travel_class",
            "flight_number",
            "legroom",
        ):
            text_value = leg_mapping.get(text_key)
            if text_value is not None and not isinstance(text_value, str):
                raise InvalidResponseError(
                    f"{collection} itinerary leg {text_key} is not a string"
                )
        extensions = leg_mapping.get("extensions")
        if extensions is not None and (
            not isinstance(extensions, list)
            or any(not isinstance(item, str) for item in cast(list[object], extensions))
        ):
            raise InvalidResponseError(
                f"{collection} itinerary leg extensions is not an array of strings"
            )
        for airport_key in ("departure_airport", "arrival_airport"):
            airport = leg_mapping.get(airport_key)
            if airport is not None and not isinstance(airport, dict):
                raise InvalidResponseError(
                    f"{collection} itinerary {airport_key} is not an object"
                )
            if isinstance(airport, dict):
                airport_mapping = cast(dict[str, object], airport)
                for text_key in ("name", "id", "time"):
                    text_value = airport_mapping.get(text_key)
                    if text_value is not None and not isinstance(text_value, str):
                        raise InvalidResponseError(
                            f"{collection} itinerary {airport_key}.{text_key} is not a string"
                        )

def request_flights(url: str) -> RequestOutcome:
    """Call SerpApi and decode JSON, retrying transient failures.

    Args:
        url: Request URL.

    Returns:
        Structured success or terminal failure information.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            opened = cast(
                HTTPResponse, urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS)
            )
            with opened as response:
                raw = response.read()
            decoded = json.loads(raw.decode("utf-8"))
            response_data = decode_response(decoded)
            metadata = response_data.get("search_metadata", {})
            fetched_at = metadata.get("processed_at") or _utc_now()
            return RequestOutcome(
                response=response_data,
                problem=None,
                search_id=metadata.get("id", ""),
                fetched_at=fetched_at,
            )
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_STATUS and exc.code < 500:
                problem = f"HTTP {exc.code} {exc.reason}"
                warn(f"  {problem} (not retryable)")
                return RequestOutcome(None, problem, "", _utc_now())
            problem = f"HTTP {exc.code}"
        except (
            HTTPException,
            TimeoutError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            InvalidResponseError,
        ) as exc:
            problem = f"{type(exc).__name__}: {exc}"

        if attempt == MAX_ATTEMPTS:
            warn(f"  gave up after {MAX_ATTEMPTS} attempts ({problem})")
            return RequestOutcome(None, problem, "", _utc_now())
        warn(f"  attempt {attempt} failed ({problem}), retrying")
        time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return RequestOutcome(None, "unexpected retry termination", "", _utc_now())

def itinerary_to_record(
    itinerary: Itinerary,
    origin: str,
    destination: str,
    day: date,
    adults: int,
    search_id: str = "",
    fetched_at: str = "",
) -> FlightRecord | None:
    """Flatten one complete itinerary into a versioned CSV record.

    Args:
        itinerary: SerpApi itinerary.
        origin: Requested origin.
        destination: Requested destination.
        day: Requested departure date.
        adults: Passenger count used for the search.
        search_id: SerpApi search identifier.
        fetched_at: Search processing timestamp.

    Returns:
        Record containing a full-segment fingerprint, or None if required fields
        are missing.
    """
    legs = itinerary.get("flights", [])
    price = itinerary.get("price")
    if not legs or price is None:
        return None
    segments = _build_segments(legs)
    if segments is None:
        return None
    first, last = legs[0], legs[-1]
    departure = first.get("departure_airport", {}).get("time", "")
    arrival = last.get("arrival_airport", {}).get("time", "")
    try:
        departed_at = parse_stamp(departure)
        arrived_at = parse_stamp(arrival)
    except ValueError:
        return None
    if price < 0 or departed_at.date() != day or arrived_at < departed_at:
        return None
    segment_text = json.dumps(segments, sort_keys=True, separators=(",", ":"))
    itinerary_id = hashlib.sha256(segment_text.encode("utf-8")).hexdigest()
    return FlightRecord(
        origin=origin,
        destination=destination,
        date=day.isoformat(),
        airline=_join_distinct([leg.get("airline", "") for leg in legs]),
        flight=_join_distinct([leg.get("flight_number", "") for leg in legs]),
        itinerary_id=itinerary_id,
        segments=segment_text,
        depart_at=departed_at.isoformat(timespec="minutes"),
        arrive_at=arrived_at.isoformat(timespec="minutes"),
        depart=format_clock(departed_at.time()),
        arrive=format_clock(arrived_at.time()),
        arrives_next_day=str(arrived_at.date() > departed_at.date()).lower(),
        aircraft=_join_distinct([leg.get("airplane", "") for leg in legs]),
        cabin=_join_distinct([leg.get("travel_class", "") for leg in legs]),
        currency="USD",
        price=int(price),
        adults=adults,
        stops=len(legs) - 1,
        notes=_join_distinct([extract_notes(leg) for leg in legs], separator="; "),
        search_id=search_id,
        fetched_at=fetched_at,
    )

def collect_records(
    response: SerpApiResponse,
    origin: str,
    destination: str,
    day: date,
    max_price: int | None,
    adults: int,
    search_id: str = "",
    fetched_at: str = "",
) -> list[FlightRecord]:
    """Convert one API response into filtered records.

    Args:
        response: Decoded SerpApi response.
        origin: Requested origin.
        destination: Requested destination.
        day: Requested departure date.
        max_price: Exact per-person cap, or None.
        adults: Passenger count used for the search.
        search_id: SerpApi search identifier.
        fetched_at: Search processing timestamp.

    Returns:
        Valid records that pass the price cap.
    """
    itineraries = [*response.get("best_flights", []), *response.get("other_flights", [])]
    records: list[FlightRecord] = []
    for itinerary in itineraries:
        record = itinerary_to_record(
            itinerary, origin, destination, day, adults, search_id, fetched_at
        )
        if record is None:
            continue
        if max_price is not None and record["price"] > max_price * adults:
            continue
        records.append(record)
    return records

def dedupe(records: list[FlightRecord]) -> list[FlightRecord]:
    """Keep the cheapest row for each full itinerary and party size.

    Args:
        records: Records in collection order.

    Returns:
        Unique records sorted by departure timestamp and party size.
    """
    cheapest: dict[RecordKey, FlightRecord] = {}
    for record in records:
        key: RecordKey = (record["itinerary_id"], record["adults"])
        existing = cheapest.get(key)
        if existing is None or record["price"] < existing["price"]:
            cheapest[key] = record
    return sorted(
        cheapest.values(),
        key=lambda record: (record["depart_at"], record["itinerary_id"], record["adults"]),
    )

def load_api_key(key_file: Path) -> str:
    """Read the API key from a file, falling back to the environment.

    Args:
        key_file: File containing the key.

    Returns:
        Key text or an empty string.
    """
    if key_file.is_file():
        key = key_file.read_text(encoding="utf-8").strip()
        if key:
            return key
        warn(f"{key_file.name} is empty, falling back to ${KEY_ENV_VAR}")
    return os.environ.get(KEY_ENV_VAR, "").strip()

def default_output_path(
    origin: str, destination: str, days: list[date], adults: int
) -> Path:
    """Derive a route/date/party-size CSV path.

    Args:
        origin: Departure airport code.
        destination: Arrival airport code.
        days: Query dates in order.
        adults: Largest party size queried.

    Returns:
        Auto-generated path under data/.
    """
    stem = f"{origin.lower()}_{destination.lower()}_{days[0].isoformat()}"
    if len(days) > 1:
        stem = f"{stem}-{days[-1].isoformat()}"
    return DATA_DIR / f"{stem}_{adults}pax.csv"

def write_csv(records: list[FlightRecord], stream: TextIO) -> None:
    """Write records using the current CSV schema.

    Args:
        records: Records to write.
        stream: Destination text stream.
    """
    writer: csv.DictWriter[str] = csv.DictWriter(
        stream, fieldnames=list(CSV_COLUMNS), extrasaction="ignore"
    )
    writer.writeheader()
    writer.writerows(records)

def write_csv_atomic(records: list[FlightRecord], destination: Path) -> None:
    """Atomically replace a CSV after fully writing a sibling temporary file.

    Args:
        records: Records to write.
        destination: Final CSV path.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            write_csv(records, cast(TextIO, handle))
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

def write_manifest_atomic(manifest: SweepManifest, destination: Path) -> None:
    """Atomically write a sweep manifest.

    Args:
        manifest: Metadata to serialize.
        destination: Final JSON path.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(manifest, handle, indent=2, sort_keys=True)
            _ = handle.write("\n")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

def build_jobs(days: list[date], cabin_codes: list[str], party_sizes: list[int]) -> list[SearchJob]:
    """Build the complete ordered search matrix.

    Args:
        days: Query dates.
        cabin_codes: SerpApi cabin codes.
        party_sizes: Passenger counts.

    Returns:
        One job per date, cabin, and party-size combination.
    """
    return [
        SearchJob(day, cabin_code, party)
        for day in days
        for cabin_code in cabin_codes
        for party in party_sizes
    ]

def fetch_jobs(
    jobs: list[SearchJob],
    origin: str,
    destination: str,
    max_stops: int | None,
    max_price: int | None,
    api_key: str,
    *,
    fresh: bool,
    deep_search: bool,
    exclude_basic: bool,
) -> SweepResult:
    """Execute a search matrix and retain query-level failures.

    Args:
        jobs: Ordered searches to execute.
        origin: Departure airport code.
        destination: Arrival airport code.
        max_stops: Maximum stops or None.
        max_price: Per-person fetch cap or None.
        api_key: SerpApi key.
        fresh: Whether to bypass cached responses.
        deep_search: Whether to request browser-equivalent results.
        exclude_basic: Whether to drop Basic Economy fares from economy searches.

    Returns:
        Collected records, failures, and success count.
    """
    records: list[FlightRecord] = []
    failures: list[str] = []
    succeeded = 0
    for index, job in enumerate(jobs):
        url = build_query(
            origin,
            destination,
            job.day,
            job.cabin_code,
            job.party,
            max_stops,
            api_key,
            fresh=fresh,
            deep_search=deep_search,
            exclude_basic=exclude_basic,
        )
        outcome = request_flights(url)
        label = f"{job.day} cabin {job.cabin_code} party {job.party}"
        if outcome.response is None:
            problem = outcome.problem or "request failed"
            failures.append(f"{label}: {problem}")
            warn(f"  {label}: FAILED ({problem})")
        else:
            error = outcome.response.get("error")
            status = outcome.response.get("search_metadata", {}).get("status")
            response_problem = error or (f"search status {status}" if status == "Error" else "")
            if response_problem:
                failures.append(f"{label}: {response_problem}")
                warn(f"  {label}: FAILED ({response_problem})")
            else:
                found = collect_records(
                    outcome.response,
                    origin,
                    destination,
                    job.day,
                    max_price,
                    job.party,
                    outcome.search_id,
                    outcome.fetched_at,
                )
                records.extend(found)
                succeeded += 1
                warn(f"  {label}: {len(found)} flights")
        if index + 1 < len(jobs):
            time.sleep(REQUEST_DELAY_SECONDS)
    return SweepResult(records, failures, succeeded)

def build_parser() -> argparse.ArgumentParser:
    """Define the command-line interface.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        description="Fetch one-way fares via SerpApi's Google Flights endpoint.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _ = parser.add_argument("origin", type=airport_code, help="departure airport, e.g. JFK")
    _ = parser.add_argument("destination", type=airport_code, help="arrival airport, e.g. SFO")
    _ = parser.add_argument("--start", type=iso_date, required=True, help="first date, YYYY-MM-DD")
    _ = parser.add_argument("--end", type=iso_date, help="last date (defaults to --start)")
    _ = parser.add_argument(
        "--cabin",
        nargs="+",
        choices=sorted(CABIN_CODES),
        default=["economy"],
        help="one or more cabins to query (default: economy)",
    )
    _ = parser.add_argument(
        "--max-price",
        type=positive_int,
        help="drop fares above this exact per-person amount",
    )
    _ = parser.add_argument(
        "--max-stops",
        type=int,
        choices=sorted(MAX_STOPS_CODES),
        help="0 for nonstop only; omit for any number of stops",
    )
    _ = parser.add_argument(
        "--adults", type=positive_int, default=1, help="largest party size (default: 1)"
    )
    _ = parser.add_argument(
        "--sweep",
        action="store_true",
        help="query every party size from 1 through --adults",
    )
    _ = parser.add_argument(
        "--allow-cache",
        action="store_true",
        help="allow cached responses during a sweep (fresh is the default)",
    )
    _ = parser.add_argument(
        "--deep-search",
        action="store_true",
        help="request slower results matching the Google Flights browser",
    )
    _ = parser.add_argument(
        "--include-basic",
        action="store_true",
        help="keep Basic Economy fares in economy searches (excluded by default)",
    )
    _ = parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="write successful searches even if part of the matrix fails",
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

def build_manifest(
    origin: str,
    destination: str,
    days: list[date],
    cabin_codes: list[str],
    max_stops: int | None,
    party_sizes: list[int],
    jobs: list[SearchJob],
    result: SweepResult,
    fresh: bool,
    deep_search: bool,
    exclude_basic: bool,
    started_at: str,
) -> SweepManifest:
    """Assemble a sidecar manifest for one fetch run.

    Args:
        origin: Departure airport code.
        destination: Arrival airport code.
        days: Query dates.
        cabin_codes: Queried cabin codes.
        max_stops: Maximum stops or None.
        party_sizes: Queried passenger counts.
        jobs: Complete search matrix.
        result: Sweep execution result.
        fresh: Whether caching was bypassed.
        deep_search: Whether deep search was enabled.
        exclude_basic: Whether Basic Economy fares were excluded from economy searches.
        started_at: Client start timestamp.

    Returns:
        Serializable manifest.
    """
    return SweepManifest(
        schema_version=SCHEMA_VERSION,
        complete=not result.failures,
        origin=origin,
        destination=destination,
        currency="USD",
        start=days[0].isoformat(),
        end=days[-1].isoformat(),
        cabin_codes=cabin_codes,
        max_stops=max_stops,
        party_sizes=party_sizes,
        searches_expected=len(jobs),
        searches_succeeded=result.searches_succeeded,
        failures=result.failures,
        fresh=fresh,
        deep_search=deep_search,
        exclude_basic=exclude_basic,
        started_at=started_at,
        finished_at=_utc_now(),
    )

def _build_segments(legs: list[LegInfo]) -> list[dict[str, str]] | None:
    """Build the full identity payload for an itinerary."""
    segments: list[dict[str, str]] = []
    for leg in legs:
        departure = leg.get("departure_airport", {})
        arrival = leg.get("arrival_airport", {})
        origin = departure.get("id", "")
        destination = arrival.get("id", "")
        departure_time = departure.get("time", "")
        arrival_time = arrival.get("time", "")
        airline = leg.get("airline", "")
        flight = leg.get("flight_number", "")
        cabin = leg.get("travel_class", "")
        if not all(
            [origin, destination, departure_time, arrival_time, airline, flight, cabin]
        ):
            return None
        segments.append(
            {
                "origin": origin,
                "destination": destination,
                "depart_at": departure_time,
                "arrive_at": arrival_time,
                "airline": airline,
                "flight": flight,
                "cabin": cabin,
            }
        )
    return segments

def _join_distinct(values: list[str], separator: str = " / ") -> str:
    """Join nonempty strings once while preserving their order."""
    return separator.join(dict.fromkeys(value for value in values if value))

def _utc_now() -> str:
    """Return the current UTC timestamp in ISO format."""
    return datetime.now(UTC).isoformat(timespec="seconds")

def format_currency(value: int) -> str:
    """Format an integer dollar value for progress output."""
    return f"${value:,}"

def config_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> FetchConfig:
    """Validate parsed arguments and assemble fetch settings.

    Args:
        parser: Active parser, used for user-facing validation errors.
        args: Parsed command-line arguments.

    Returns:
        Validated fetch configuration.
    """
    origin = cast(str, args.origin)
    destination = cast(str, args.destination)
    if origin == destination:
        parser.error("origin and destination must differ")
    start = cast(date, args.start)
    end = cast(date | None, args.end) or start
    try:
        days = daterange(start, end)
    except ValueError as exc:
        parser.error(str(exc))

    adults = cast(int, args.adults)
    sweep = cast(bool, args.sweep)
    party_sizes = list(range(1, adults + 1)) if sweep else [adults]
    cabins = cast(list[str], args.cabin)
    cabin_codes = sorted({CABIN_CODES[name] for name in cabins})
    jobs = build_jobs(days, cabin_codes, party_sizes)
    return FetchConfig(
        origin=origin,
        destination=destination,
        days=days,
        adults=adults,
        party_sizes=party_sizes,
        cabin_codes=cabin_codes,
        jobs=jobs,
        max_stops=cast(int | None, args.max_stops),
        max_price=cast(int | None, args.max_price),
        fresh=sweep and not cast(bool, args.allow_cache),
        deep_search=cast(bool, args.deep_search),
        exclude_basic=not cast(bool, args.include_basic),
        allow_partial=cast(bool, args.allow_partial),
        key_file=Path(cast(str, args.key_file)),
        output=cast(str | None, args.output),
    )

def execute_fetch(
    config: FetchConfig, api_key: str
) -> tuple[SweepResult, list[FlightRecord], str]:
    """Run a configured search matrix.

    Args:
        config: Validated fetch settings.
        api_key: SerpApi credential.

    Returns:
        Raw sweep result, deduplicated records, and start timestamp.
    """
    plan = (
        f"{len(config.days)} dates x {len(config.cabin_codes)} cabins x "
        f"{len(config.party_sizes)} party sizes"
    )
    warn(f"{config.origin} -> {config.destination}: {plan} = {len(config.jobs)} requests")
    sweeping = len(config.party_sizes) > 1
    fetch_cap = None if sweeping else config.max_price
    if sweeping and config.max_price is not None:
        warn(f"  --sweep: {format_currency(config.max_price)} cap deferred to analysis")
    if config.fresh:
        warn("  --sweep: bypassing cached results to reduce snapshot skew")
    if config.exclude_basic and ECONOMY_CODE in config.cabin_codes:
        warn("  economy: Basic Economy fares excluded (--include-basic keeps them)")

    started_at = _utc_now()
    result = fetch_jobs(
        config.jobs,
        config.origin,
        config.destination,
        config.max_stops,
        fetch_cap,
        api_key,
        fresh=config.fresh,
        deep_search=config.deep_search,
        exclude_basic=config.exclude_basic,
    )
    unique = dedupe(result.records)
    warn(f"{len(unique)} unique itinerary fares after dedupe")
    return (result, unique, started_at)

def write_fetch_result(
    config: FetchConfig,
    result: SweepResult,
    records: list[FlightRecord],
    started_at: str,
) -> int:
    """Apply completeness policy and publish fetch outputs.

    Args:
        config: Validated fetch settings.
        result: Query-level sweep result.
        records: Deduplicated output rows.
        started_at: Client start timestamp.

    Returns:
        Process exit status.
    """
    if result.failures and not config.allow_partial:
        warn(
            f"refusing to write an incomplete sweep ({len(result.failures)} failed searches); "
            "use --allow-partial to override"
        )
        return 1

    manifest = build_manifest(
        config.origin,
        config.destination,
        config.days,
        config.cabin_codes,
        config.max_stops,
        config.party_sizes,
        config.jobs,
        result,
        config.fresh,
        config.deep_search,
        config.exclude_basic,
        started_at,
    )
    if config.output == "-":
        write_csv(records, sys.stdout)
        warn(
            f"stdout output complete={manifest['complete']} "
            f"({manifest['searches_succeeded']}/{manifest['searches_expected']} searches)"
        )
        return 0

    destination_path = (
        Path(config.output)
        if config.output
        else default_output_path(
            config.origin, config.destination, config.days, config.adults
        )
    )
    write_csv_atomic(records, destination_path)
    sidecar = manifest_path(destination_path)
    write_manifest_atomic(manifest, sidecar)
    warn(f"wrote {destination_path}")
    warn(f"wrote {sidecar}")
    return 0

def main() -> int:
    parser = build_parser()
    config = config_from_args(parser, parser.parse_args())
    try:
        api_key = load_api_key(config.key_file)
    except OSError as exc:
        warn(f"Unable to read API key: {exc}")
        return 1
    if not api_key:
        warn(f"No API key found in the key file or ${KEY_ENV_VAR}.")
        return 1
    result, records, started_at = execute_fetch(config, api_key)
    try:
        return write_fetch_result(config, result, records, started_at)
    except OSError as exc:
        warn(f"Unable to write fetch output: {exc}")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
