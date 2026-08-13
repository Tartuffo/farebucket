"""Shared fare arithmetic, CSV loading, validation, and display helpers."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path
from typing import cast

FILENAME_PATTERN = re.compile(r"^([a-z]{3})_([a-z]{3})_")
LEGACY_REQUIRED_COLUMNS = frozenset(
    {
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
    }
)

class FareDataError(ValueError):
    """Raised when a fare dataset is malformed or internally inconsistent."""

@dataclass(frozen=True)
class DatasetInfo:
    """Route and collection metadata associated with a fare CSV."""

    source: Path
    origin: str | None
    destination: str | None
    currency: str
    complete: bool | None
    schema_version: int | None

    @property
    def route(self) -> str:
        """Return a human-readable route label."""
        if self.origin and self.destination:
            return f"{self.origin} -> {self.destination}"
        return "??? -> ???"

@dataclass(frozen=True)
class Flight:
    """One physical itinerary with fares collected for several party sizes."""

    flight_date: date
    origin: str | None
    destination: str | None
    airline: str
    flight: str
    itinerary_id: str
    depart_at: datetime
    arrive_at: datetime
    aircraft: str
    cabin: str
    stops: int
    notes: str
    currency: str
    segments: str
    fares: dict[int, int]
    search_ids: dict[int, str]
    fetched_at: dict[int, str]

    @property
    def depart(self) -> time:
        """Return the local departure time."""
        return self.depart_at.time()

    @property
    def arrive(self) -> time:
        """Return the local arrival time."""
        return self.arrive_at.time()

    @property
    def arrival_day_offset(self) -> int:
        """Return the number of calendar days between departure and arrival."""
        return (self.arrive_at.date() - self.depart_at.date()).days

    @property
    def arrives_next_day(self) -> bool:
        """Return whether arrival occurs after the departure date."""
        return self.arrival_day_offset > 0

    def seat_fare(self, seats: int) -> Fraction | None:
        """Return the exact observed per-seat fare for a party-size search.

        Args:
            seats: Party size used for the search.

        Returns:
            The party total divided by its seat count, or None when unavailable.
        """
        total = self.fares.get(seats)
        return Fraction(total, seats) if total is not None else None

    def together_total(self, party: int) -> int | None:
        """Return the observed total for booking the whole party together.

        Args:
            party: Number of passengers.

        Returns:
            The observed total, or None when that search is unavailable.
        """
        return self.fares.get(party)

    def split_bound_total(self, party: int) -> Fraction | None:
        """Return the exact snapshot-model upper bound for separate bookings.

        Args:
            party: Number of passengers.

        Returns:
            The sum of observed per-seat fares for search sizes 1 through party,
            or None if any required search is unavailable.
        """
        fares = [self.seat_fare(seats) for seats in range(1, party + 1)]
        if any(fare is None for fare in fares):
            return None
        return sum(cast(list[Fraction], fares), start=Fraction())

    def split_ceiling_total(self, party: int) -> int | None:
        """Return the split bound rounded upward to a whole currency unit.

        Args:
            party: Number of passengers.

        Returns:
            A conservative whole-unit split total, or None when unavailable.
        """
        bound = self.split_bound_total(party)
        return ceil_fraction(bound) if bound is not None else None

    def best_total(self, party: int) -> int | None:
        """Return the lower conservative whole-unit booking total.

        Args:
            party: Number of passengers.

        Returns:
            The cheaper of booking together and the rounded-up split bound.
        """
        options = [
            total
            for total in (self.together_total(party), self.split_ceiling_total(party))
            if total is not None
        ]
        return min(options) if options else None

    def best_per_person(self, party: int) -> Fraction | None:
        """Return the conservative best total divided across the party.

        Args:
            party: Number of passengers.

        Returns:
            Per-person cost, or None when the party cannot be priced.
        """
        total = self.best_total(party)
        return Fraction(total, party) if total is not None else None

    def split_saving(self, party: int) -> int:
        """Return the whole-unit saving supported by the snapshot model.

        Args:
            party: Number of passengers.

        Returns:
            A nonnegative conservative saving.
        """
        together = self.together_total(party)
        split = self.split_ceiling_total(party)
        if together is None or split is None:
            return 0
        return max(0, together - split)

    def wants_split(self, party: int) -> bool:
        """Return whether separate bookings have a positive modeled saving.

        Args:
            party: Number of passengers.

        Returns:
            True when the conservative whole-unit saving is positive.
        """
        return self.split_saving(party) > 0

@dataclass(frozen=True)
class FareDataset:
    """Flights and metadata loaded from one fare CSV."""

    info: DatasetInfo
    flights: list[Flight]

@dataclass(frozen=True)
class ParsedFareRow:
    """Validated intermediate representation of one CSV row."""

    flight_date: date
    origin: str | None
    destination: str | None
    airline: str
    flight: str
    itinerary_id: str
    depart_at: datetime
    arrive_at: datetime
    aircraft: str
    cabin: str
    stops: int
    notes: str
    currency: str
    segments: str
    price: int
    adults: int
    search_id: str
    fetched_at: str

    @property
    def group_key(self) -> tuple[date, str, str]:
        """Return the key used to join party-size observations."""
        return (self.flight_date, self.itinerary_id, self.cabin)

    @property
    def signature(self) -> tuple[object, ...]:
        """Return fields that must agree within a party-size group."""
        return (
            self.flight_date,
            self.origin,
            self.destination,
            self.airline,
            self.flight,
            self.itinerary_id,
            self.depart_at,
            self.arrive_at,
            self.aircraft,
            self.cabin,
            self.stops,
            self.notes,
            self.currency,
            self.segments,
        )

def ceil_fraction(value: Fraction) -> int:
    """Round a nonnegative fraction upward to an integer.

    Args:
        value: Fraction to round.

    Returns:
        The mathematical ceiling.
    """
    return -(-value.numerator // value.denominator)

def format_money(value: Fraction | int, *, always_cents: bool = False) -> str:
    """Format a currency amount using conventional half-up cent rounding.

    Args:
        value: Currency amount.
        always_cents: Whether integer amounts should include `.00`.

    Returns:
        A dollar-prefixed display string.
    """
    fraction = value if isinstance(value, Fraction) else Fraction(value)
    decimal = Decimal(fraction.numerator) / Decimal(fraction.denominator)
    rounded = decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if not always_cents and rounded == rounded.to_integral_value():
        return f"${int(rounded):,}"
    return f"${rounded:,.2f}"

def format_clock(value: time) -> str:
    """Format a time without platform-specific strftime directives.

    Args:
        value: Time of day.

    Returns:
        A 12-hour clock such as `6:05 AM`.
    """
    hour = value.hour % 12 or 12
    meridiem = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {meridiem}"

def arrival_suffix(flight: Flight) -> str:
    """Return a day-offset suffix for an arrival.

    Args:
        flight: Flight whose arrival is being displayed.

    Returns:
        An empty string for same-day arrivals or `+N` otherwise.
    """
    offset = flight.arrival_day_offset
    return f"+{offset}" if offset else ""

def group_by_date(flights: list[Flight]) -> dict[date, list[Flight]]:
    """Group flights by departure date in schedule order.

    Args:
        flights: Flights to group.

    Returns:
        Mapping from date to flights sorted by departure time.
    """
    grouped: dict[date, list[Flight]] = defaultdict(list)
    for flight in flights:
        grouped[flight.flight_date].append(flight)
    for same_day in grouped.values():
        same_day.sort(key=lambda flight: flight.depart_at)
    return dict(grouped)

def manifest_path(csv_path: Path) -> Path:
    """Return the sidecar manifest path for a CSV.

    Args:
        csv_path: Fare CSV path.

    Returns:
        Path ending in `.csv.meta.json`.
    """
    return csv_path.with_suffix(f"{csv_path.suffix}.meta.json")

def infer_route_from_filename(path: Path) -> tuple[str | None, str | None]:
    """Infer airport codes from a generated fare filename.

    Args:
        path: Fare CSV path.

    Returns:
        Origin and destination, or two None values when unavailable.
    """
    match = FILENAME_PATTERN.match(path.name)
    if not match:
        return (None, None)
    return (match.group(1).upper(), match.group(2).upper())

def load_dataset(path: Path) -> FareDataset:
    """Load and validate a current or legacy fare CSV.

    Args:
        path: CSV produced by fetch_flights.py.

    Returns:
        Validated flights and dataset metadata.

    Raises:
        FareDataError: If the CSV or its manifest is malformed.
        OSError: If either file cannot be read.
    """
    manifest = _load_manifest(path)
    inferred_origin, inferred_destination = infer_route_from_filename(path)
    manifest_origin = _manifest_text(manifest, "origin")
    manifest_destination = _manifest_text(manifest, "destination")
    default_origin = manifest_origin or inferred_origin
    default_destination = manifest_destination or inferred_destination

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        missing = sorted(LEGACY_REQUIRED_COLUMNS - fields)
        if missing:
            raise FareDataError(f"{path}: missing CSV columns: {', '.join(missing)}")
        raw_rows = cast(list[dict[str, str | None]], list(reader))

    parsed = [
        _parse_row(row, path, row_number, default_origin, default_destination)
        for row_number, row in enumerate(raw_rows, start=2)
    ]
    flights = _build_flights(parsed, path)
    origin = _single_value("origin", [row.origin for row in parsed], default_origin, path)
    destination = _single_value(
        "destination", [row.destination for row in parsed], default_destination, path
    )
    currency = _single_value(
        "currency", [row.currency for row in parsed], _manifest_text(manifest, "currency") or "USD", path
    )
    complete = _manifest_bool(manifest, "complete")
    schema_version = _manifest_int(manifest, "schema_version")
    return FareDataset(
        info=DatasetInfo(
            source=path,
            origin=origin,
            destination=destination,
            currency=currency or "USD",
            complete=complete,
            schema_version=schema_version,
        ),
        flights=flights,
    )

def load_flights(path: Path) -> list[Flight]:
    """Load only the flights from a fare CSV.

    Args:
        path: Fare CSV path.

    Returns:
        Validated flights.
    """
    return load_dataset(path).flights

def _load_manifest(path: Path) -> dict[str, object] | None:
    """Read a sidecar manifest when present."""
    sidecar = manifest_path(path)
    if not sidecar.exists():
        return None
    try:
        decoded = json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FareDataError(f"{sidecar}: invalid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise FareDataError(f"{sidecar}: manifest must contain a JSON object")
    return cast(dict[str, object], decoded)

def _manifest_text(manifest: dict[str, object] | None, key: str) -> str | None:
    """Read an optional string field from a manifest."""
    if manifest is None or key not in manifest:
        return None
    value = manifest[key]
    if not isinstance(value, str):
        raise FareDataError(f"manifest field {key!r} must be a string")
    return value

def _manifest_bool(manifest: dict[str, object] | None, key: str) -> bool | None:
    """Read an optional Boolean field from a manifest."""
    if manifest is None or key not in manifest:
        return None
    value = manifest[key]
    if not isinstance(value, bool):
        raise FareDataError(f"manifest field {key!r} must be a boolean")
    return value

def _manifest_int(manifest: dict[str, object] | None, key: str) -> int | None:
    """Read an optional integer field from a manifest."""
    if manifest is None or key not in manifest:
        return None
    value = manifest[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise FareDataError(f"manifest field {key!r} must be an integer")
    return value

def _single_value(
    label: str,
    values: list[str | None],
    fallback: str | None,
    path: Path,
) -> str | None:
    """Resolve a single consistent dataset-level string value."""
    present = {value for value in values if value}
    if fallback:
        present.add(fallback)
    if len(present) > 1:
        raise FareDataError(f"{path}: inconsistent {label} values: {sorted(present)}")
    return next(iter(present), None)

def _parse_row(
    row: dict[str, str | None],
    path: Path,
    row_number: int,
    default_origin: str | None,
    default_destination: str | None,
) -> ParsedFareRow:
    """Validate and convert one raw CSV row."""
    context = f"{path}:{row_number}"
    try:
        flight_date = date.fromisoformat(_cell(row, "date", context))
        adults = int(_cell(row, "adults", context))
        price = int(_cell(row, "price", context))
        stops = int(_cell(row, "stops", context))
        if adults <= 0:
            raise FareDataError(f"{context}: adults must be positive")
        if price < 0:
            raise FareDataError(f"{context}: price cannot be negative")
        if stops < 0:
            raise FareDataError(f"{context}: stops cannot be negative")
        depart_at, arrive_at = _parse_timestamps(row, flight_date, context)
        if arrive_at < depart_at:
            raise FareDataError(f"{context}: arrival precedes departure")
    except ValueError as exc:
        if isinstance(exc, FareDataError):
            raise
        raise FareDataError(f"{context}: invalid value: {exc}") from exc

    origin = _optional_cell(row, "origin") or default_origin
    destination = _optional_cell(row, "destination") or default_destination
    cabin = _cell(row, "cabin", context).replace(" Class", "")
    flight = _cell(row, "flight", context)
    depart_text = depart_at.isoformat(timespec="minutes")
    arrive_text = arrive_at.isoformat(timespec="minutes")
    itinerary_id = _optional_cell(row, "itinerary_id") or _legacy_itinerary_id(
        flight_date, flight, depart_text, arrive_text, cabin, stops
    )
    return ParsedFareRow(
        flight_date=flight_date,
        origin=origin,
        destination=destination,
        airline=_cell(row, "airline", context),
        flight=flight,
        itinerary_id=itinerary_id,
        depart_at=depart_at,
        arrive_at=arrive_at,
        aircraft=_optional_cell(row, "aircraft"),
        cabin=cabin,
        stops=stops,
        notes=_optional_cell(row, "notes"),
        currency=_optional_cell(row, "currency") or "USD",
        segments=_optional_cell(row, "segments"),
        price=price,
        adults=adults,
        search_id=_optional_cell(row, "search_id"),
        fetched_at=_optional_cell(row, "fetched_at"),
    )

def _parse_timestamps(
    row: dict[str, str | None], flight_date: date, context: str
) -> tuple[datetime, datetime]:
    """Parse current ISO timestamps or reconstruct legacy timestamps."""
    depart_raw = _optional_cell(row, "depart_at")
    arrive_raw = _optional_cell(row, "arrive_at")
    if depart_raw or arrive_raw:
        if not depart_raw or not arrive_raw:
            raise FareDataError(f"{context}: depart_at and arrive_at must both be present")
        depart_at = datetime.fromisoformat(depart_raw)
        arrive_at = datetime.fromisoformat(arrive_raw)
        if depart_at.date() != flight_date:
            raise FareDataError(f"{context}: depart_at date does not match date column")
        return (depart_at, arrive_at)

    depart = datetime.strptime(_cell(row, "depart", context), "%I:%M %p").time()
    arrive = datetime.strptime(_cell(row, "arrive", context), "%I:%M %p").time()
    next_day_raw = _cell(row, "arrives_next_day", context).lower()
    if next_day_raw not in {"true", "false"}:
        raise FareDataError(f"{context}: arrives_next_day must be true or false")
    depart_at = datetime.combine(flight_date, depart)
    arrival_date = flight_date + timedelta(days=next_day_raw == "true")
    return (depart_at, datetime.combine(arrival_date, arrive))

def _build_flights(rows: list[ParsedFareRow], path: Path) -> list[Flight]:
    """Collapse validated party-size rows into Flight objects."""
    grouped: dict[tuple[date, str, str], list[ParsedFareRow]] = defaultdict(list)
    for row in rows:
        grouped[row.group_key].append(row)

    flights: list[Flight] = []
    for group in grouped.values():
        head = group[0]
        if any(row.signature != head.signature for row in group[1:]):
            raise FareDataError(
                f"{path}: itinerary {head.itinerary_id} has inconsistent party-size metadata"
            )
        fares: dict[int, int] = {}
        search_ids: dict[int, str] = {}
        fetched_at: dict[int, str] = {}
        for row in group:
            if row.adults in fares:
                raise FareDataError(
                    f"{path}: duplicate fare for itinerary {row.itinerary_id}, party {row.adults}"
                )
            fares[row.adults] = row.price
            search_ids[row.adults] = row.search_id
            fetched_at[row.adults] = row.fetched_at
        flights.append(
            Flight(
                flight_date=head.flight_date,
                origin=head.origin,
                destination=head.destination,
                airline=head.airline,
                flight=head.flight,
                itinerary_id=head.itinerary_id,
                depart_at=head.depart_at,
                arrive_at=head.arrive_at,
                aircraft=head.aircraft,
                cabin=head.cabin,
                stops=head.stops,
                notes=head.notes.split(";")[0].strip(),
                currency=head.currency,
                segments=head.segments,
                fares=fares,
                search_ids=search_ids,
                fetched_at=fetched_at,
            )
        )
    return sorted(flights, key=lambda flight: (flight.flight_date, flight.depart_at, flight.flight))

def _legacy_itinerary_id(
    flight_date: date,
    flight: str,
    depart_at: str,
    arrive_at: str,
    cabin: str,
    stops: int,
) -> str:
    """Build the strongest identity available in the legacy CSV schema."""
    payload = "|".join(
        [flight_date.isoformat(), flight, depart_at, arrive_at, cabin, str(stops)]
    )
    return f"legacy-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

def _cell(row: dict[str, str | None], key: str, context: str) -> str:
    """Read a required nonempty CSV cell."""
    value = row.get(key)
    if value is None or not value.strip():
        raise FareDataError(f"{context}: missing {key}")
    return value.strip()

def _optional_cell(row: dict[str, str | None], key: str) -> str:
    """Read an optional CSV cell as stripped text."""
    value = row.get(key)
    return value.strip() if value else ""
