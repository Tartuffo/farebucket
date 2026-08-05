"""Render a swept fare CSV as a per-day table with one column per party size.

For a party of N, let f_k be the cheapest per-seat fare in a bucket holding at
least k seats, so f_k = P(k) / k. These rise monotonically with k. Booking the
party together costs N * f_N. Booking seats in separate transactions costs at
most sum(f_1..f_N), because after k-1 seats are taken the f_k bucket provably
still has one left. The reported price is the cheaper of the two, marked with
an asterisk when splitting wins.

Examples:
    uv run python summarize.py data/sfo_jfk_2026-12-04_3pax.csv \\
        --parties 2 3 --earliest-depart 08:00 --max-price 1600
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import TypedDict, cast

FILENAME_PATTERN = re.compile(r"^([a-z]{3})_([a-z]{3})_")


class FareRow(TypedDict):
    date: str
    airline: str
    flight: str
    depart: str
    arrive: str
    arrives_next_day: str
    aircraft: str
    cabin: str
    price: str
    adults: str
    stops: str
    notes: str


@dataclass(frozen=True)
class Flight:
    flight_date: date
    airline: str
    flight: str
    depart: time
    arrive: time
    arrives_next_day: bool
    aircraft: str
    cabin: str
    stops: int
    notes: str
    fares: dict[int, int]

    def seat_fare(self, seats: int) -> int | None:
        """Per-seat price in the cheapest bucket holding at least `seats`."""
        total = self.fares.get(seats)
        return round(total / seats) if total is not None else None

    def together(self, party: int) -> int | None:
        """Per-person cost with the whole party on one reservation."""
        return self.seat_fare(party)

    def split(self, party: int) -> int | None:
        """Per-person cost bound when each seat is bought separately."""
        fares = [self.seat_fare(k) for k in range(1, party + 1)]
        if any(fare is None for fare in fares):
            return None
        return round(sum(cast(list[int], fares)) / party)

    def best(self, party: int) -> int | None:
        """Cheaper of booking together versus booking separately."""
        options = [value for value in (self.together(party), self.split(party)) if value]
        return min(options) if options else None

    def wants_split(self, party: int) -> bool:
        """True when separate reservations beat one shared reservation."""
        together, split = self.together(party), self.split(party)
        return together is not None and split is not None and split < together


def parse_clock(raw: str) -> time:
    """Parse a clock cell such as '6:00 AM'.

    Args:
        raw: Time text.

    Returns:
        Parsed time of day.
    """
    return datetime.strptime(raw.strip(), "%I:%M %p").time()


def load_flights(path: Path) -> list[Flight]:
    """Read a swept CSV, collapsing party-size rows into one record per flight.

    Args:
        path: CSV produced by fetch_flights.py.

    Returns:
        One Flight per distinct flight, carrying every party-size fare.
    """
    with path.open(newline="", encoding="utf-8") as handle:
        rows = cast(list[FareRow], list(csv.DictReader(handle)))

    grouped: dict[tuple[str, str, str], list[FareRow]] = defaultdict(list)
    for row in rows:
        grouped[(row["date"], row["flight"], row["depart"])].append(row)

    flights: list[Flight] = []
    for group in grouped.values():
        head = group[0]
        flights.append(
            Flight(
                flight_date=date.fromisoformat(head["date"]),
                airline=head["airline"],
                flight=head["flight"],
                depart=parse_clock(head["depart"]),
                arrive=parse_clock(head["arrive"]),
                arrives_next_day=head["arrives_next_day"].lower() == "true",
                aircraft=head["aircraft"],
                cabin=head["cabin"].replace(" Class", ""),
                stops=int(head["stops"]),
                notes=head["notes"].split(";")[0].strip(),
                fares={int(row["adults"]): int(row["price"]) for row in group},
            )
        )
    return sorted(flights, key=lambda f: (f.flight_date, f.depart))


def route_from_filename(path: Path) -> str:
    """Recover the route label from the generated filename.

    Args:
        path: CSV path such as sfo_jfk_2026-12-04_3pax.csv.

    Returns:
        A label like 'SFO -> JFK', or a placeholder when unparseable.
    """
    match = FILENAME_PATTERN.match(path.name)
    if not match:
        return "??? -> ???"
    return f"{match.group(1).upper()} -> {match.group(2).upper()}"


def keeps(
    flight: Flight,
    parties: list[int],
    max_price: int | None,
    earliest: time | None,
    latest: time | None,
    same_day: bool,
) -> bool:
    """Apply the schedule and budget constraints to one flight.

    Args:
        flight: Candidate flight.
        parties: Party sizes being reported.
        max_price: Per-person cap, applied to the cheapest party column.
        earliest: Earliest acceptable departure.
        latest: Latest acceptable arrival.
        same_day: Whether next-day arrivals are rejected.

    Returns:
        True when the flight satisfies every constraint.
    """
    if same_day and flight.arrives_next_day:
        return False
    if earliest and flight.depart < earliest:
        return False
    if latest and not flight.arrives_next_day and flight.arrive > latest:
        return False
    if max_price is not None:
        prices = [flight.best(p) for p in parties]
        usable = [value for value in prices if value is not None]
        if not usable or min(usable) > max_price:
            return False
    return True


def render(flights: list[Flight], parties: list[int], route: str, day: date) -> str:
    """Build the fixed-width table.

    Args:
        flights: Flights to show, already filtered and sorted.
        parties: Party sizes to include as price columns.
        route: Route label.
        day: The date being reported.

    Returns:
        The formatted table.
    """
    price_columns = [f"PPx{p}" for p in parties]
    columns = ("Airline", "Flight", "Depart", "Arrive", "Aircraft", "Cabin", *price_columns, "Notes")

    cells: list[tuple[str, ...]] = []
    for f in flights:
        prices: list[str] = []
        for party in parties:
            value = f.best(party)
            prices.append(f"${value:,}" + ("*" if f.wants_split(party) else "") if value else "-")
        cells.append(
            (
                f.airline,
                f.flight,
                f.depart.strftime("%-I:%M %p"),
                f.arrive.strftime("%-I:%M %p") + ("+1" if f.arrives_next_day else ""),
                f.aircraft,
                f.cabin,
                *prices,
                f.notes,
            )
        )

    widths = [
        max(len(columns[i]), max((len(row[i]) for row in cells), default=0))
        for i in range(len(columns))
    ]

    def line(values: tuple[str, ...]) -> str:
        parts = [
            values[i].rjust(widths[i])
            if columns[i].startswith("PPx")
            else values[i].ljust(widths[i])
            for i in range(len(columns))
        ]
        return "  ".join(parts).rstrip()

    header = f"{route}  | {day:%a %b} {day.day}, {day.year}"
    rule = "  ".join("-" * w for w in widths)
    body = [line(row) for row in cells] or ["(nothing meets the constraints)"]
    return "\n".join([header, "", line(columns), rule, *body])


def build_parser() -> argparse.ArgumentParser:
    """Define the command line interface.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(description="Summarize a swept fare CSV.")
    _ = parser.add_argument("csv_path", help="CSV produced by fetch_flights.py")
    _ = parser.add_argument(
        "--parties", nargs="+", type=int, default=[2], help="party sizes to price (default: 2)"
    )
    _ = parser.add_argument("--max-price", type=int, help="per-person cap")
    _ = parser.add_argument("--earliest-depart", help="earliest departure, HH:MM")
    _ = parser.add_argument("--latest-arrive", help="latest same-day arrival, HH:MM")
    _ = parser.add_argument(
        "--same-day", action="store_true", help="reject flights arriving the next day"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    path = Path(cast(str, args.csv_path))
    parties = sorted(cast(list[int], args.parties))
    max_price = cast(int | None, args.max_price)
    same_day = cast(bool, args.same_day)
    earliest_raw = cast(str | None, args.earliest_depart)
    latest_raw = cast(str | None, args.latest_arrive)
    earliest = time.fromisoformat(earliest_raw) if earliest_raw else None
    latest = time.fromisoformat(latest_raw) if latest_raw else None

    if not path.exists():
        print(f"no such file: {path}")
        return 1

    flights = load_flights(path)
    if not flights:
        print(f"{path} has no rows")
        return 1

    kept = [f for f in flights if keeps(f, parties, max_price, earliest, latest, same_day)]
    dropped = [f for f in flights if f not in kept]
    route = route_from_filename(path)

    print(render(kept, parties, route, flights[0].flight_date))
    print("\n* cheaper booked as separate reservations")

    if dropped:
        print(f"\nExcluded ({len(dropped)}):")
        for f in sorted(dropped, key=lambda x: x.depart):
            price = f.best(parties[0])
            shown = f"${price:,}" if price else "n/a"
            arrive = f.arrive.strftime("%-I:%M %p") + ("+1" if f.arrives_next_day else "")
            schedule = f"{f.depart:%-I:%M %p} -> {arrive}"
            print(f"  {f.airline:<9} {f.flight:<8} {schedule:<22} {shown:>7}/pp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
