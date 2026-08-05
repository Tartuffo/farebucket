"""Rank round-trip pairings by total airfare for a given number of full days.

A full day is one spent entirely at the destination, so both travel days are
excluded and the return date is the departure date plus full_days plus one.
Optionally the stay must contain an anchor date, such as a holiday.

Fare and split-booking math lives in summarize.py; this module pairs and ranks.

Example:
    uv run python analyze_trips.py \\
        data/jfk_sfo_2026-11-16-2026-11-25_3pax.csv \\
        data/sfo_jfk_2026-11-27-2026-12-06_3pax.csv \\
        --full-days 9 10 --passengers 3 --max-price 1600 \\
        --must-include 2026-11-26 --same-day-outbound \\
        --earliest-return-departure 08:00
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, time, timedelta
from pathlib import Path
from typing import cast

from summarize import Flight, load_flights, render

type DatePair = tuple[date, date]


@dataclass(frozen=True)
class Criteria:
    """Everything that constrains which flights and pairings are acceptable."""

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
    outbound: Flight
    inbound: Flight
    full_days: int
    passengers: int

    @property
    def per_person(self) -> int:
        out = self.outbound.best(self.passengers) or 0
        back = self.inbound.best(self.passengers) or 0
        return out + back

    @property
    def total(self) -> int:
        return self.per_person * self.passengers

    @property
    def needs_split(self) -> bool:
        return self.outbound.wants_split(self.passengers) or self.inbound.wants_split(
            self.passengers
        )

    @property
    def dates(self) -> DatePair:
        return (self.outbound.flight_date, self.inbound.flight_date)

    @property
    def split_saving(self) -> int:
        """Total saved across the party by booking seats separately."""
        saving = 0
        for leg in (self.outbound, self.inbound):
            together = leg.together(self.passengers)
            split = leg.split(self.passengers)
            if together is not None and split is not None and split < together:
                saving += (together - split) * self.passengers
        return saving


def within_budget(flight: Flight, criteria: Criteria) -> bool:
    """Check price and stop constraints shared by both directions.

    Args:
        flight: Candidate flight.
        criteria: Active constraints.

    Returns:
        True when the flight is bookable for the party within budget.
    """
    price = flight.best(criteria.passengers)
    if price is None or flight.stops > criteria.max_stops:
        return False
    return criteria.max_price is None or price <= criteria.max_price


def passes_outbound_filters(flight: Flight, criteria: Criteria) -> bool:
    """Check an outbound flight against the trip's constraints.

    Args:
        flight: Candidate outbound flight.
        criteria: Active constraints.

    Returns:
        True if the flight is affordable and lands within the cutoff.
    """
    if not within_budget(flight, criteria):
        return False
    if criteria.same_day_outbound and flight.arrives_next_day:
        return False
    if criteria.depart_from and flight.flight_date < criteria.depart_from:
        return False
    if criteria.latest_outbound_arrival and not flight.arrives_next_day:
        return flight.arrive <= criteria.latest_outbound_arrival
    return True


def passes_return_filters(flight: Flight, criteria: Criteria) -> bool:
    """Check a return flight against the trip's constraints.

    Args:
        flight: Candidate return flight.
        criteria: Active constraints.

    Returns:
        True if the flight is affordable and departs within the window.
    """
    if not within_budget(flight, criteria):
        return False
    if criteria.same_day_return and flight.arrives_next_day:
        return False
    if criteria.earliest_return_departure:
        return flight.depart >= criteria.earliest_return_departure
    return True


def group_by_date(flights: list[Flight]) -> dict[date, list[Flight]]:
    """Bucket flights by departure date.

    Args:
        flights: Flights to group.

    Returns:
        Mapping of date to that date's flights, in schedule order.
    """
    grouped: dict[date, list[Flight]] = defaultdict(list)
    for flight in flights:
        grouped[flight.flight_date].append(flight)
    for same_day in grouped.values():
        same_day.sort(key=lambda f: f.depart)
    return dict(grouped)


def find_trip_options(
    outbound: list[Flight], inbound: list[Flight], full_days: int, criteria: Criteria
) -> list[TripOption]:
    """Build the cheapest pairing for each valid date combination.

    Reporting one option per date pair keeps results genuinely different trips
    rather than several near-identical variants on the same two dates.

    Args:
        outbound: Filtered outbound flights.
        inbound: Filtered return flights.
        full_days: Complete days required at the destination.
        criteria: Active constraints.

    Returns:
        The cheapest option for each date pair, cheapest first.
    """
    returns_by_date = group_by_date(inbound)
    cheapest: dict[DatePair, TripOption] = {}
    for out_flight in outbound:
        return_date = out_flight.flight_date + timedelta(days=full_days + 1)
        anchor = criteria.must_include
        if anchor and not out_flight.flight_date < anchor < return_date:
            continue
        for in_flight in returns_by_date.get(return_date, []):
            option = TripOption(out_flight, in_flight, full_days, criteria.passengers)
            existing = cheapest.get(option.dates)
            if existing is None or option.per_person < existing.per_person:
                cheapest[option.dates] = option
    return sorted(cheapest.values(), key=lambda o: (o.per_person, o.dates))


def format_leg(label: str, flight: Flight, passengers: int) -> str:
    """Render one leg of a trip option as a single line.

    Args:
        label: Short prefix such as 'Out' or 'Back'.
        flight: The flight to render.
        passengers: Party size, for pricing.

    Returns:
        A one-line summary of the leg.
    """
    arrive = flight.arrive.strftime("%H:%M")
    if flight.arrives_next_day:
        arrive += "+1"
    price = flight.best(passengers) or 0
    marker = "*" if flight.wants_split(passengers) else ""
    fields = [
        f"     {label:<4}",
        f"{flight.flight_date:%a %b} {flight.flight_date.day}",
        f"{flight.airline} {flight.flight}",
        f"{flight.depart:%H:%M} / {arrive}",
        flight.aircraft,
        flight.cabin,
        f"${price:,}{marker}",
    ]
    return "  ".join(fields)


def describe_option(option: TripOption, rank: int) -> str:
    """Render a single trip option as an indented block.

    Args:
        option: The pairing to describe.
        rank: 1-based position in the cheapest list.

    Returns:
        A multi-line description.
    """
    flags: list[str] = []
    if option.needs_split:
        flags.append(f"book seats separately, saves ${option.split_saving:,}")
    if option.inbound.arrives_next_day:
        flags.append("red-eye return")
    suffix = f"  [{'; '.join(flags)}]" if flags else ""
    headline = (
        f"  {rank}. ${option.per_person:,}/person  |  "
        f"${option.total:,} for {option.passengers}{suffix}"
    )
    return "\n".join(
        [
            headline,
            format_leg("Out", option.outbound, option.passengers),
            format_leg("Back", option.inbound, option.passengers),
        ]
    )


def print_dump(route: str, flights: list[Flight], passengers: int) -> None:
    """Print qualifying flights grouped by day.

    Args:
        route: Route label.
        flights: Filtered flights for that direction.
        passengers: Party size, for pricing.
    """
    grouped = group_by_date(flights)
    for day in sorted(grouped):
        print(render(grouped[day], [passengers], route, day))
        print()


def build_parser() -> argparse.ArgumentParser:
    """Define the command line interface.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        description="Rank round-trip pairings by total airfare.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _ = parser.add_argument("outbound", help="outbound CSV from fetch_flights.py")
    _ = parser.add_argument("inbound", help="return CSV from fetch_flights.py")
    _ = parser.add_argument(
        "--full-days",
        nargs="+",
        type=int,
        required=True,
        help="day counts to evaluate, e.g. --full-days 9 10",
    )
    _ = parser.add_argument("--passengers", type=int, default=1, help="party size (default: 1)")
    _ = parser.add_argument("--max-price", type=int, help="per-person cap")
    _ = parser.add_argument(
        "--max-stops", type=int, default=0, help="maximum stops per leg (default: 0)"
    )
    _ = parser.add_argument(
        "--options", type=int, default=3, help="options to show per scenario (default: 3)"
    )
    _ = parser.add_argument(
        "--must-include", help="date the stay must contain, e.g. a holiday (YYYY-MM-DD)"
    )
    _ = parser.add_argument("--depart-from", help="earliest acceptable outbound date")
    _ = parser.add_argument(
        "--same-day-outbound", action="store_true", help="outbound must land the same day"
    )
    _ = parser.add_argument(
        "--same-day-return", action="store_true", help="reject red-eye returns"
    )
    _ = parser.add_argument("--latest-outbound-arrival", help="latest arrival, HH:MM")
    _ = parser.add_argument("--earliest-return-departure", help="earliest departure, HH:MM")
    _ = parser.add_argument(
        "--dump", action="store_true", help="also print every qualifying flight by day"
    )
    return parser


def criteria_from_args(args: argparse.Namespace) -> Criteria:
    """Translate parsed arguments into a Criteria record.

    Args:
        args: Parsed command line arguments.

    Returns:
        The assembled constraints.
    """
    must_include = cast(str | None, args.must_include)
    depart_from = cast(str | None, args.depart_from)
    latest_arrival = cast(str | None, args.latest_outbound_arrival)
    earliest_departure = cast(str | None, args.earliest_return_departure)
    return Criteria(
        passengers=cast(int, args.passengers),
        full_days=tuple(cast(list[int], args.full_days)),
        options=cast(int, args.options),
        max_price=cast(int | None, args.max_price),
        max_stops=cast(int, args.max_stops),
        must_include=date.fromisoformat(must_include) if must_include else None,
        depart_from=date.fromisoformat(depart_from) if depart_from else None,
        same_day_outbound=cast(bool, args.same_day_outbound),
        same_day_return=cast(bool, args.same_day_return),
        latest_outbound_arrival=time.fromisoformat(latest_arrival) if latest_arrival else None,
        earliest_return_departure=(
            time.fromisoformat(earliest_departure) if earliest_departure else None
        ),
    )


def describe_criteria(criteria: Criteria) -> list[str]:
    """Summarize the active constraints for the report header.

    Args:
        criteria: Active constraints.

    Returns:
        One line per constraint in effect.
    """
    lines = [f"{criteria.passengers} passenger(s), max {criteria.max_stops} stop(s)"]
    if criteria.max_price:
        lines.append(f"Cap ${criteria.max_price:,}/person.")
    if criteria.must_include:
        anchor = criteria.must_include
        lines.append(f"Stay must include {anchor:%a %b} {anchor.day}.")
    if criteria.depart_from:
        start = criteria.depart_from
        lines.append(f"Departing on or after {start:%a %b} {start.day}.")
    if criteria.same_day_outbound:
        lines.append("Outbound must land the same day.")
    if criteria.latest_outbound_arrival:
        lines.append(f"Outbound arrives by {criteria.latest_outbound_arrival:%H:%M}.")
    if criteria.earliest_return_departure:
        lines.append(f"Return departs {criteria.earliest_return_departure:%H:%M} or later.")
    if criteria.same_day_return:
        lines.append("Red-eye returns excluded.")
    return lines


def main() -> int:
    args = build_parser().parse_args()
    criteria = criteria_from_args(args)
    outbound_path = Path(cast(str, args.outbound))
    inbound_path = Path(cast(str, args.inbound))

    for path in (outbound_path, inbound_path):
        if not path.exists():
            print(f"missing data file: {path}")
            return 1

    outbound = [f for f in load_flights(outbound_path) if passes_outbound_filters(f, criteria)]
    inbound = [f for f in load_flights(inbound_path) if passes_return_filters(f, criteria)]

    print("=" * 74)
    for entry in describe_criteria(criteria):
        print(entry)
    print("=" * 74)
    print(f"\n{len(outbound)} qualifying outbound flights, {len(inbound)} qualifying returns")

    if cast(bool, args.dump):
        print()
        print_dump("OUTBOUND", outbound, criteria.passengers)
        print_dump("RETURN", inbound, criteria.passengers)

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

    print("\n* cheaper booked as separate reservations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
