"""Rank round-trip pairings by conservative total airfare."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, time, timedelta
from fractions import Fraction
from pathlib import Path
from typing import cast

from fare_data import (
    DatasetInfo,
    FareDataError,
    Flight,
    arrival_suffix,
    format_money,
    group_by_date,
    load_dataset,
)
from summarize import iso_clock, iso_date, positive_int, render

type DatePair = tuple[date, date]

@dataclass(frozen=True)
class Criteria:
    """Constraints controlling acceptable flights and trip pairings."""

    passengers: int
    full_days: tuple[int, ...]
    options: int
    max_price: int | None = None
    max_stops: int = 0
    must_include: date | None = None
    depart_from: date | None = None
    same_day_outbound: bool = False
    same_day_return: bool = False
    latest_outbound_arrival: time | None = None
    earliest_return_departure: time | None = None

@dataclass(frozen=True)
class TripOption:
    """One outbound and inbound pairing for a requested stay length."""

    outbound: Flight
    inbound: Flight
    full_days: int
    passengers: int

    @property
    def total(self) -> int:
        """Return the conservative whole-unit total for both directions."""
        outbound_total = self.outbound.best_total(self.passengers)
        inbound_total = self.inbound.best_total(self.passengers)
        if outbound_total is None or inbound_total is None:
            raise ValueError("trip option contains an unpriced flight")
        return outbound_total + inbound_total

    @property
    def per_person(self) -> Fraction:
        """Return exact per-person cost from the conservative total."""
        return Fraction(self.total, self.passengers)

    @property
    def needs_split(self) -> bool:
        """Return whether either direction benefits from separate bookings."""
        return self.outbound.wants_split(self.passengers) or self.inbound.wants_split(
            self.passengers
        )

    @property
    def dates(self) -> DatePair:
        """Return outbound and inbound departure dates."""
        return (self.outbound.flight_date, self.inbound.flight_date)

    @property
    def split_saving(self) -> int:
        """Return the conservative whole-unit saving across both directions."""
        return self.outbound.split_saving(self.passengers) + self.inbound.split_saving(
            self.passengers
        )

def nonnegative_int(raw: str) -> int:
    """Parse a nonnegative command-line integer.

    Args:
        raw: Argument text.

    Returns:
        A nonnegative integer.

    Raises:
        argparse.ArgumentTypeError: If the value is negative or not an integer.
    """
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("value cannot be negative")
    return value

def is_priced(flight: Flight, criteria: Criteria) -> bool:
    """Check availability and stop constraints shared by both directions.

    Args:
        flight: Candidate flight.
        criteria: Active constraints.

    Returns:
        True when the party can be priced and the stop limit is satisfied.
    """
    return (
        flight.best_total(criteria.passengers) is not None
        and flight.stops <= criteria.max_stops
    )

def passes_outbound_filters(flight: Flight, criteria: Criteria) -> bool:
    """Check an outbound flight against schedule constraints.

    Args:
        flight: Candidate outbound flight.
        criteria: Active constraints.

    Returns:
        True when the flight satisfies every outbound constraint.
    """
    if not is_priced(flight, criteria):
        return False
    if criteria.same_day_outbound and flight.arrives_next_day:
        return False
    if criteria.depart_from and flight.flight_date < criteria.depart_from:
        return False
    if criteria.latest_outbound_arrival:
        if flight.arrives_next_day:
            return False
        if flight.arrive > criteria.latest_outbound_arrival:
            return False
    return True

def passes_return_filters(flight: Flight, criteria: Criteria) -> bool:
    """Check a return flight against schedule constraints.

    Args:
        flight: Candidate return flight.
        criteria: Active constraints.

    Returns:
        True when the flight satisfies every return constraint.
    """
    if not is_priced(flight, criteria):
        return False
    if criteria.same_day_return and flight.arrives_next_day:
        return False
    if criteria.earliest_return_departure:
        return flight.depart >= criteria.earliest_return_departure
    return True

def find_trip_options(
    outbound: list[Flight], inbound: list[Flight], full_days: int, criteria: Criteria
) -> list[TripOption]:
    """Build the cheapest valid pairing for each date combination.

    Args:
        outbound: Filtered outbound flights.
        inbound: Filtered return flights.
        full_days: Complete days required at the destination.
        criteria: Active constraints.

    Returns:
        One cheapest option per date pair, ordered by total price.
    """
    returns_by_date = group_by_date(inbound)
    cheapest: dict[DatePair, TripOption] = {}
    for out_flight in outbound:
        arrival_date = out_flight.arrive_at.date()
        return_date = arrival_date + timedelta(days=full_days + 1)
        anchor = criteria.must_include
        if anchor and not arrival_date < anchor < return_date:
            continue
        for in_flight in returns_by_date.get(return_date, []):
            option = TripOption(out_flight, in_flight, full_days, criteria.passengers)
            if (
                criteria.max_price is not None
                and option.total > criteria.max_price * criteria.passengers
            ):
                continue
            existing = cheapest.get(option.dates)
            if existing is None or option.total < existing.total:
                cheapest[option.dates] = option
    return sorted(cheapest.values(), key=lambda option: (option.total, option.dates))

def format_leg(label: str, flight: Flight, passengers: int) -> str:
    """Render one direction of a trip option.

    Args:
        label: Short prefix such as `Out` or `Back`.
        flight: Flight to render.
        passengers: Party size used for pricing.

    Returns:
        One-line flight summary.
    """
    price = flight.best_per_person(passengers)
    shown_price = format_money(price, always_cents=True) if price is not None else "n/a"
    marker = "*" if flight.wants_split(passengers) else ""
    fields = [
        f"     {label:<4}",
        f"{flight.flight_date:%a %b} {flight.flight_date.day}",
        f"{flight.airline} {flight.flight}",
        f"{flight.depart:%H:%M} / {flight.arrive:%H:%M}{arrival_suffix(flight)}",
        flight.aircraft,
        flight.cabin,
        f"{shown_price}{marker}",
    ]
    return "  ".join(fields)

def describe_option(option: TripOption, rank: int) -> str:
    """Render one ranked trip option.

    Args:
        option: Pairing to describe.
        rank: One-based ranking.

    Returns:
        Multi-line option description.
    """
    flags: list[str] = []
    if option.needs_split:
        flags.append(f"separate bookings save at least {format_money(option.split_saving)}")
    if option.inbound.arrives_next_day:
        flags.append("red-eye return")
    suffix = f"  [{'; '.join(flags)}]" if flags else ""
    headline = (
        f"  {rank}. {format_money(option.per_person, always_cents=True)}/person  |  "
        f"{format_money(option.total)} for {option.passengers}{suffix}"
    )
    return "\n".join(
        [
            headline,
            format_leg("Out", option.outbound, option.passengers),
            format_leg("Back", option.inbound, option.passengers),
        ]
    )

def print_dump(route: str, flights: list[Flight], passengers: int) -> None:
    """Print qualifying flights grouped by departure date.

    Args:
        route: Route label.
        flights: Filtered flights for one direction.
        passengers: Party-size fare column to display.
    """
    grouped = group_by_date(flights)
    for day in sorted(grouped):
        print(render(grouped[day], [passengers], route, day))
        print()

def build_parser() -> argparse.ArgumentParser:
    """Define the command-line interface.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        description="Rank round-trip pairings by conservative total airfare.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _ = parser.add_argument("outbound", help="outbound CSV from fetch_flights.py")
    _ = parser.add_argument("inbound", help="return CSV from fetch_flights.py")
    _ = parser.add_argument(
        "--full-days",
        nargs="+",
        type=nonnegative_int,
        required=True,
        help="full day counts to evaluate, e.g. --full-days 6 7",
    )
    _ = parser.add_argument(
        "--passengers", type=positive_int, default=1, help="party size (default: 1)"
    )
    _ = parser.add_argument(
        "--max-price",
        type=positive_int,
        help="maximum round-trip price per person",
    )
    _ = parser.add_argument(
        "--max-stops", type=nonnegative_int, default=0, help="maximum stops per leg (default: 0)"
    )
    _ = parser.add_argument(
        "--options", type=positive_int, default=3, help="options to show per scenario (default: 3)"
    )
    _ = parser.add_argument(
        "--must-include",
        type=iso_date,
        help="full date the stay must contain (YYYY-MM-DD)",
    )
    _ = parser.add_argument(
        "--depart-from", type=iso_date, help="earliest acceptable outbound date"
    )
    _ = parser.add_argument(
        "--same-day-outbound", action="store_true", help="outbound must land the same day"
    )
    _ = parser.add_argument(
        "--same-day-return", action="store_true", help="reject red-eye returns"
    )
    _ = parser.add_argument(
        "--latest-outbound-arrival",
        type=iso_clock,
        help="latest same-day arrival, HH:MM",
    )
    _ = parser.add_argument(
        "--earliest-return-departure", type=iso_clock, help="earliest departure, HH:MM"
    )
    _ = parser.add_argument(
        "--allow-partial-data",
        action="store_true",
        help="analyze datasets whose manifests say their sweeps are incomplete",
    )
    _ = parser.add_argument(
        "--dump", action="store_true", help="also print every qualifying flight by day"
    )
    return parser

def criteria_from_args(args: argparse.Namespace) -> Criteria:
    """Translate parsed arguments into a Criteria record.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Assembled constraints.
    """
    full_days = tuple(dict.fromkeys(cast(list[int], args.full_days)))
    return Criteria(
        passengers=cast(int, args.passengers),
        full_days=full_days,
        options=cast(int, args.options),
        max_price=cast(int | None, args.max_price),
        max_stops=cast(int, args.max_stops),
        must_include=cast(date | None, args.must_include),
        depart_from=cast(date | None, args.depart_from),
        same_day_outbound=cast(bool, args.same_day_outbound),
        same_day_return=cast(bool, args.same_day_return),
        latest_outbound_arrival=cast(time | None, args.latest_outbound_arrival),
        earliest_return_departure=cast(time | None, args.earliest_return_departure),
    )

def describe_criteria(criteria: Criteria) -> list[str]:
    """Summarize active constraints for the report header.

    Args:
        criteria: Active constraints.

    Returns:
        One line per active constraint.
    """
    lines = [f"{criteria.passengers} passenger(s), max {criteria.max_stops} stop(s)"]
    if criteria.max_price is not None:
        lines.append(f"Round-trip cap {format_money(criteria.max_price)}/person.")
    if criteria.must_include:
        anchor = criteria.must_include
        lines.append(f"Stay must fully include {anchor:%a %b} {anchor.day}.")
    if criteria.depart_from:
        start = criteria.depart_from
        lines.append(f"Departing on or after {start:%a %b} {start.day}.")
    if criteria.same_day_outbound:
        lines.append("Outbound must land the same day.")
    if criteria.latest_outbound_arrival:
        lines.append(f"Outbound arrives the same day by {criteria.latest_outbound_arrival:%H:%M}.")
    if criteria.earliest_return_departure:
        lines.append(f"Return departs {criteria.earliest_return_departure:%H:%M} or later.")
    if criteria.same_day_return:
        lines.append("Red-eye returns excluded.")
    return lines

def validate_dataset_pair(outbound: DatasetInfo, inbound: DatasetInfo) -> str | None:
    """Validate that two datasets form a reverse-route pair.

    Args:
        outbound: Outbound dataset metadata.
        inbound: Inbound dataset metadata.

    Returns:
        An error message when incompatible, otherwise None.
    """
    if outbound.currency != inbound.currency:
        return f"currency mismatch: {outbound.currency} outbound, {inbound.currency} inbound"
    route_known = all(
        [outbound.origin, outbound.destination, inbound.origin, inbound.destination]
    )
    if route_known and (
        outbound.origin != inbound.destination or outbound.destination != inbound.origin
    ):
        return f"routes are not reverses: {outbound.route} and {inbound.route}"
    return None

def _load_cli_dataset(
    parser: argparse.ArgumentParser, path: Path, allow_partial: bool
) -> tuple[DatasetInfo, list[Flight]]:
    """Load one CLI dataset and enforce completeness policy."""
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
    if not dataset.flights:
        parser.error(f"{path} contains no fare rows")
    return (dataset.info, dataset.flights)

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    criteria = criteria_from_args(args)
    allow_partial = cast(bool, args.allow_partial_data)
    outbound_path = Path(cast(str, args.outbound))
    inbound_path = Path(cast(str, args.inbound))
    outbound_info, outbound_all = _load_cli_dataset(parser, outbound_path, allow_partial)
    inbound_info, inbound_all = _load_cli_dataset(parser, inbound_path, allow_partial)
    incompatibility = validate_dataset_pair(outbound_info, inbound_info)
    if incompatibility:
        parser.error(incompatibility)

    outbound = [
        flight for flight in outbound_all if passes_outbound_filters(flight, criteria)
    ]
    inbound = [flight for flight in inbound_all if passes_return_filters(flight, criteria)]
    print("=" * 74)
    for entry in describe_criteria(criteria):
        print(entry)
    print("=" * 74)
    print(
        f"\n{len(outbound)} schedule-qualifying outbound flights, "
        f"{len(inbound)} schedule-qualifying returns"
    )

    if cast(bool, args.dump):
        print()
        print_dump(outbound_info.route, outbound, criteria.passengers)
        print_dump(inbound_info.route, inbound, criteria.passengers)

    for full_days in criteria.full_days:
        options = find_trip_options(outbound, inbound, full_days, criteria)
        print(f"\n{'-' * 74}\n{full_days} FULL DAYS AT DESTINATION\n{'-' * 74}")
        if not options:
            print("  (no pairing satisfies this requirement)")
            continue
        for rank, option in enumerate(options[: criteria.options], start=1):
            print(describe_option(option, rank))
            print()
        print(f"  ({len(options)} valid date pairings in total)")

    print("\n* separate bookings have a lower snapshot-model upper bound")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
