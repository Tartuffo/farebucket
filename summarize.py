"""Render swept fare data as per-day tables of observed per-seat prices."""

from __future__ import annotations

import argparse
from datetime import date, time
from fractions import Fraction
from pathlib import Path
from typing import cast

from fare_data import (
    DatasetInfo,
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
    latest_depart: time | None = None,
) -> bool:
    """Apply schedule and budget constraints to one flight.

    Args:
        flight: Candidate flight.
        parties: Party sizes being reported.
        max_price: Per-person cap, applied to the cheapest requested party.
        earliest: Earliest acceptable departure.
        latest: Latest acceptable same-day arrival.
        same_day: Whether later-day arrivals are rejected.
        latest_depart: Latest acceptable departure, inclusive.

    Returns:
        True when the flight satisfies every constraint.
    """
    if same_day and flight.arrives_next_day:
        return False
    if earliest and flight.depart < earliest:
        return False
    if latest_depart and flight.depart > latest_depart:
        return False
    if latest and (flight.arrives_next_day or flight.arrive > latest):
        return False
    if max_price is not None:
        prices = [flight.best_per_person(party) for party in parties]
        usable = [price for price in prices if price is not None]
        if not usable or min(usable) > Fraction(max_price):
            return False
    return True

def price_cells(flight: Flight, parties: list[int], *, always_cents: bool) -> list[str]:
    """Format one flight's per-seat fares, starring party sizes worth splitting.

    Args:
        flight: Flight being displayed.
        parties: Party-size searches to show.
        always_cents: Whether whole-dollar fares should include `.00`.

    Returns:
        One cell per party size, `-` where no fare was observed.
    """
    cells: list[str] = []
    for party in parties:
        value = flight.seat_fare(party)
        if value is None:
            cells.append("-")
            continue
        marker = "*" if flight.wants_split(party) else ""
        cells.append(f"{format_money(value, always_cents=always_cents)}{marker}")
    return cells

def align(
    columns: tuple[str, ...], cells: list[tuple[str, ...]], right_aligned: range
) -> list[str]:
    """Lay out a header, rule, and rows in padded columns.

    Args:
        columns: Column titles.
        cells: Row values in column order.
        right_aligned: Indexes of the right-justified columns.

    Returns:
        Header line, rule line, then one line per row.
    """
    widths = [
        max(len(columns[index]), max((len(row[index]) for row in cells), default=0))
        for index in range(len(columns))
    ]

    def line(values: tuple[str, ...]) -> str:
        parts = [
            values[index].rjust(widths[index])
            if index in right_aligned
            else values[index].ljust(widths[index])
            for index in range(len(columns))
        ]
        return "  ".join(parts).rstrip()

    rule = "  ".join("-" * width for width in widths)
    return [line(columns), rule, *(line(row) for row in cells)]

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
    cells: list[tuple[str, ...]] = [
        (
            flight.airline,
            flight.flight,
            format_clock(flight.depart),
            f"{format_clock(flight.arrive)}{arrival_suffix(flight)}",
            flight.aircraft,
            flight.cabin,
            *price_cells(flight, parties, always_cents=True),
            flight.notes,
        )
        for flight in flights
    ]
    header = f"{route}  | {day:%a %b} {day.day}, {day.year}"
    table = align(columns, cells, range(6, 6 + len(parties)))
    if not cells:
        table.append("(nothing meets the constraints)")
    return "\n".join([header, "", *table])

def describe_filters(
    max_price: int | None,
    earliest: time | None,
    latest_depart: time | None,
    latest_arrive: time | None,
) -> list[str]:
    """Describe the active constraints in words for an emailed table.

    Args:
        max_price: Per-person cap.
        earliest: Earliest acceptable departure.
        latest_depart: Latest acceptable departure.
        latest_arrive: Latest acceptable same-day arrival.

    Returns:
        Short phrases such as `departing 9:00 AM to 5:00 PM`.
    """
    phrases: list[str] = []
    if earliest and latest_depart:
        phrases.append(f"departing {format_clock(earliest)} to {format_clock(latest_depart)}")
    elif earliest:
        phrases.append(f"departing {format_clock(earliest)} or later")
    elif latest_depart:
        phrases.append(f"departing by {format_clock(latest_depart)}")
    if latest_arrive:
        phrases.append(f"arriving by {format_clock(latest_arrive)}")
    if max_price is not None:
        phrases.append(f"up to {format_money(max_price)} per person")
    return phrases

def render_email(
    flights: list[Flight],
    parties: list[int],
    info: DatasetInfo,
    day: date,
    filters: list[str],
) -> str:
    """Build a narrow plain-text fare table for pasting into an email.

    Args:
        flights: Flights from a single date, already filtered and sorted.
        parties: Party-size searches to show as per-seat columns.
        info: Dataset metadata supplying the route and fare type.
        day: Departure date being reported.
        filters: Phrases describing the active constraints.

    Returns:
        Formatted text that fits an unwrapped email line.
    """
    scope = [*sorted({flight.cabin for flight in flights}), *filters]
    if flights and all(flight.stops == 0 for flight in flights):
        scope.insert(0, "Nonstop")
    pricing = "Price per person"
    if info.exclude_basic is not None:
        pricing += f", Basic Economy fares {'excluded' if info.exclude_basic else 'included'}"
    price_columns = ["Price"] if parties == [1] else [f"{party} pax" for party in parties]
    columns = ("Depart", "Arrive", "Flight", *price_columns)
    cells: list[tuple[str, ...]] = [
        (
            format_clock(flight.depart),
            f"{format_clock(flight.arrive)}{arrival_suffix(flight)}",
            f"{flight.airline} {flight.flight}",
            *price_cells(flight, parties, always_cents=False),
        )
        for flight in flights
    ]
    heading = [f"{info.route} | {day:%a %b} {day.day}, {day.year}"]
    if scope:
        heading.append(", ".join(scope))
    if not cells:
        return "\n".join([*heading, "", "(no flights meet these constraints)"])
    lines = [*heading, pricing, "", *align(columns, cells, range(3, len(columns)))]
    if any(cell.endswith("*") for row in cells for cell in row[3:]):
        lines.extend(["", "* may be cheaper booked as separate reservations"])
    return "\n".join(lines)

def fares_as_of(flights: list[Flight]) -> date | None:
    """Find the oldest search date behind a set of flights.

    Args:
        flights: Flights being reported.

    Returns:
        Earliest UTC fetch date, or None when the CSV does not record one.
    """
    days: list[date] = []
    for flight in flights:
        for stamp in flight.fetched_at.values():
            try:
                days.append(date.fromisoformat(stamp[:10]))
            except ValueError:
                continue
    return min(days, default=None)

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
    _ = parser.add_argument("--latest-depart", type=iso_clock, help="latest departure, HH:MM")
    _ = parser.add_argument("--latest-arrive", type=iso_clock, help="latest same-day arrival, HH:MM")
    _ = parser.add_argument(
        "--same-day", action="store_true", help="reject flights arriving after departure day"
    )
    _ = parser.add_argument(
        "--email",
        action="store_true",
        help="print a narrow plain-text table for pasting into an email",
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

def print_email(
    info: DatasetInfo,
    source_flights: list[Flight],
    kept_flights: list[Flight],
    parties: list[int],
    filters: list[str],
) -> None:
    """Print one email-ready fare table per source date.

    Args:
        info: Dataset metadata supplying the route and fare type.
        source_flights: Flights before constraint filtering.
        kept_flights: Flights that passed the constraints.
        parties: Party-size columns to render.
        filters: Phrases describing the active constraints.
    """
    kept_by_date = group_by_date(kept_flights)
    for day in sorted(group_by_date(source_flights)):
        print(render_email(kept_by_date.get(day, []), parties, info, day, filters))
        print()
    checked = fares_as_of(kept_flights)
    if checked:
        print(f"Fares as of {checked:%b} {checked.day}, {checked.year} (UTC); prices change often.")

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
    latest_depart = cast(time | None, args.latest_depart)
    kept = [
        flight
        for flight in flights
        if keeps(flight, parties, max_price, earliest, latest, same_day, latest_depart)
    ]
    if cast(bool, args.email):
        filters = describe_filters(max_price, earliest, latest_depart, latest)
        print_email(dataset.info, flights, kept, parties, filters)
        return 0
    print_summary_tables(dataset.info.route, flights, kept, parties)
    print("\n* split snapshot bound is lower than booking the party together")
    print_excluded([flight for flight in flights if flight not in kept], parties)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
