"""Render swept fare data as per-day tables of observed per-seat prices."""

from __future__ import annotations

import argparse
from datetime import date, time
from fractions import Fraction
from pathlib import Path
from typing import cast

from fare_data import (
    FareDataError,
    FareDataset,
    Flight,
    arrival_suffix,
    format_clock,
    format_money,
    group_by_date,
    load_dataset,
)

def positive_int(raw: str) -> int:
    """Parse a positive command-line integer.

    Args:
        raw: Argument text.

    Returns:
        A positive integer.

    Raises:
        argparse.ArgumentTypeError: If the value is not positive.
    """
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return value

def iso_date(raw: str) -> date:
    """Parse an ISO date command-line value.

    Args:
        raw: Date text in YYYY-MM-DD format.

    Returns:
        Parsed date.

    Raises:
        argparse.ArgumentTypeError: If the value is not an ISO date.
    """
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {raw!r}") from exc

def iso_clock(raw: str) -> time:
    """Parse an ISO clock command-line value.

    Args:
        raw: Time text in HH:MM format.

    Returns:
        Parsed time.

    Raises:
        argparse.ArgumentTypeError: If the value is not a clock time.
    """
    try:
        parsed = time.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected HH:MM, got {raw!r}") from exc
    if parsed.second or parsed.microsecond:
        raise argparse.ArgumentTypeError("time must use HH:MM precision")
    return parsed

def keeps(
    flight: Flight,
    parties: list[int],
    max_price: int | None,
    earliest: time | None,
    latest: time | None,
    same_day: bool,
) -> bool:
    """Apply schedule and budget constraints to one flight.

    Args:
        flight: Candidate flight.
        parties: Party sizes being reported.
        max_price: Per-person cap, applied to the cheapest requested party.
        earliest: Earliest acceptable departure.
        latest: Latest acceptable same-day arrival.
        same_day: Whether later-day arrivals are rejected.

    Returns:
        True when the flight satisfies every constraint.
    """
    if same_day and flight.arrives_next_day:
        return False
    if earliest and flight.depart < earliest:
        return False
    if latest and (flight.arrives_next_day or flight.arrive > latest):
        return False
    if max_price is not None:
        prices = [flight.best_per_person(party) for party in parties]
        usable = [price for price in prices if price is not None]
        if not usable or min(usable) > Fraction(max_price):
            return False
    return True

def render(flights: list[Flight], parties: list[int], route: str, day: date) -> str:
    """Build a fixed-width per-seat fare table for one departure date.

    Args:
        flights: Flights from a single date, already filtered and sorted.
        parties: Party-size searches to show as per-seat columns.
        route: Route label.
        day: Departure date being reported.

    Returns:
        Formatted table text.
    """
    price_columns = [f"PP@{party}" for party in parties]
    columns = (
        "Airline",
        "Flight",
        "Depart",
        "Arrive",
        "Aircraft",
        "Cabin",
        *price_columns,
        "Notes",
    )
    cells: list[tuple[str, ...]] = []
    for flight in flights:
        prices: list[str] = []
        for party in parties:
            value = flight.seat_fare(party)
            if value is None:
                prices.append("-")
                continue
            marker = "*" if flight.wants_split(party) else ""
            prices.append(f"{format_money(value, always_cents=True)}{marker}")
        cells.append(
            (
                flight.airline,
                flight.flight,
                format_clock(flight.depart),
                f"{format_clock(flight.arrive)}{arrival_suffix(flight)}",
                flight.aircraft,
                flight.cabin,
                *prices,
                flight.notes,
            )
        )

    widths = [
        max(len(columns[index]), max((len(row[index]) for row in cells), default=0))
        for index in range(len(columns))
    ]

    def line(values: tuple[str, ...]) -> str:
        parts = [
            values[index].rjust(widths[index])
            if columns[index].startswith("PP@")
            else values[index].ljust(widths[index])
            for index in range(len(columns))
        ]
        return "  ".join(parts).rstrip()

    header = f"{route}  | {day:%a %b} {day.day}, {day.year}"
    rule = "  ".join("-" * width for width in widths)
    body = [line(row) for row in cells] or ["(nothing meets the constraints)"]
    return "\n".join([header, "", line(columns), rule, *body])

def build_parser() -> argparse.ArgumentParser:
    """Define the command-line interface.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(description="Summarize a swept fare CSV by date.")
    _ = parser.add_argument("csv_path", help="CSV produced by fetch_flights.py")
    _ = parser.add_argument(
        "--date",
        type=iso_date,
        help="show one departure date, YYYY-MM-DD (default: show every date)",
    )
    _ = parser.add_argument(
        "--parties",
        nargs="+",
        type=positive_int,
        help="party-size searches to show (default: every size in the CSV)",
    )
    _ = parser.add_argument("--max-price", type=positive_int, help="per-person cap")
    _ = parser.add_argument("--earliest-depart", type=iso_clock, help="earliest departure, HH:MM")
    _ = parser.add_argument("--latest-arrive", type=iso_clock, help="latest same-day arrival, HH:MM")
    _ = parser.add_argument(
        "--same-day", action="store_true", help="reject flights arriving after departure day"
    )
    _ = parser.add_argument(
        "--allow-partial-data",
        action="store_true",
        help="analyze a dataset whose manifest says its sweep is incomplete",
    )
    return parser

def _available_parties(flights: list[Flight]) -> list[int]:
    """Return every party size present in a set of flights."""
    return sorted({party for flight in flights for party in flight.fares})

def load_cli_dataset(
    parser: argparse.ArgumentParser, path: Path, allow_partial: bool
) -> FareDataset:
    """Load one CLI dataset and enforce its completeness policy.

    Args:
        parser: Active parser for reporting input errors.
        path: Fare CSV path.
        allow_partial: Whether an incomplete manifest is acceptable.

    Returns:
        Validated fare dataset.
    """
    if not path.is_file():
        parser.error(f"not a readable fare CSV: {path}")
    try:
        dataset = load_dataset(path)
    except (FareDataError, OSError) as exc:
        parser.error(str(exc))
    if dataset.info.complete is False and not allow_partial:
        parser.error(
            f"{path} is marked as an incomplete sweep; pass --allow-partial-data to continue"
        )
    return dataset

def print_summary_tables(
    route: str,
    source_flights: list[Flight],
    kept_flights: list[Flight],
    parties: list[int],
) -> None:
    """Print one correctly labeled fare table per source date.

    Args:
        route: Route label.
        source_flights: Flights before constraint filtering.
        kept_flights: Flights that passed the constraints.
        parties: Party-size columns to render.
    """
    kept_by_date = group_by_date(kept_flights)
    source_by_date = group_by_date(source_flights)
    for index, day in enumerate(sorted(source_by_date)):
        if index:
            print()
        print(render(kept_by_date.get(day, []), parties, route, day))

def print_excluded(flights: list[Flight], parties: list[int]) -> None:
    """Print concise details for flights rejected by active constraints.

    Args:
        flights: Rejected flights.
        parties: Party sizes used to choose the displayed price.
    """
    if not flights:
        return
    print(f"\nExcluded ({len(flights)}):")
    for flight in sorted(flights, key=lambda item: item.depart_at):
        prices = [flight.best_per_person(party) for party in parties]
        usable = [price for price in prices if price is not None]
        shown = format_money(min(usable), always_cents=True) if usable else "n/a"
        schedule = (
            f"{format_clock(flight.depart)} -> "
            f"{format_clock(flight.arrive)}{arrival_suffix(flight)}"
        )
        print(
            f"  {flight.flight_date}  {flight.airline:<9} {flight.flight:<14} "
            f"{schedule:<25} {shown:>10}/pp"
        )

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    path = Path(cast(str, args.csv_path))
    dataset = load_cli_dataset(parser, path, cast(bool, args.allow_partial_data))
    if not dataset.flights:
        print(f"{path} has no fare rows")
        return 1
    requested_parties = cast(list[int] | None, args.parties)
    parties = sorted(set(requested_parties or _available_parties(dataset.flights)))
    selected_date = cast(date | None, args.date)
    flights = [
        flight
        for flight in dataset.flights
        if selected_date is None or flight.flight_date == selected_date
    ]
    if not flights:
        print(f"{path} has no flights for {selected_date}")
        return 1
    max_price = cast(int | None, args.max_price)
    earliest = cast(time | None, args.earliest_depart)
    latest = cast(time | None, args.latest_arrive)
    same_day = cast(bool, args.same_day)
    kept = [
        flight
        for flight in flights
        if keeps(flight, parties, max_price, earliest, latest, same_day)
    ]
    print_summary_tables(dataset.info.route, flights, kept, parties)
    print("\n* split snapshot bound is lower than booking the party together")
    print_excluded([flight for flight in flights if flight not in kept], parties)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
