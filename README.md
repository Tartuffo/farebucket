# farebucket

Finds the cheap seats a booking engine hides when you ask for more than one.

## The problem

Search a flight for one passenger and you might see $180. Search the same
flight for two and the per-person price jumps to $208. Nothing changed except
your party size.

That's because airlines sell seats in **fare buckets**, each holding a limited
number of seats. When you book several passengers on one reservation, the
airline prices everyone in the cheapest bucket deep enough to hold the whole
party. One orphan seat at $180 is invisible to a two-person search.

Real economy fares from `data/`, JFK↔SFO over Thanksgiving 2026:

| Flight  | Date   | 1 seat | 2 seats | 3 seats | Seats in the cheap bucket |
|---------|--------|--------|---------|---------|---------------------------|
| B6 1515 | Nov 24 | $180   | $359    | $539    | at least 3                |
| AA 177  | Nov 24 | $180   | $417    | $626    | exactly 1                 |
| AS 1542 | Nov 22 | $340   | $679    | $1,166  | exactly 2                 |
| DL 669  | Nov 29 | $829   | $1,657  | $3,131  | exactly 2                 |

Look at the first two rows. Search either flight for one passenger and you see
**the same $180**. They are not the same. B6 1515 has at least three seats at
that price; AA 177 has exactly one, and a second passenger reprices *both* to
$208.

The last row is the expensive case. Nov 29 is the Sunday after Thanksgiving,
the busiest travel day of the year. Two seats cost $828 each, but asking for
three moves the whole party to $1,044 each. Book a pair and a single instead
and you pay $1,657 + $1,044 = $2,701 rather than $3,131 — **$430 saved on the
same three seats on the same flight**.

None of this is visible from a normal search, because a search for three
people never shows you the price for one.

`farebucket` finds these automatically by querying every party size from 1 to
N and comparing. In the bundled sample, 16 of 240 flights reward splitting.

## How it works

For a party of N, let `f(k)` be the cheapest per-seat fare in a bucket holding
at least `k` seats, so `f(k) = P(k) / k`. These rise monotonically.

```
booking together:    N * f(N)
booking separately:  at most  f(1) + f(2) + ... + f(N)
```

The separate-booking figure is an **upper bound**, not an estimate: after
`k-1` seats are taken, the `f(k)` bucket provably still has one left. So the
reported saving is a floor.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). No third-party
Python packages — everything runs on the standard library.

### Getting an API key

Fare data comes from [SerpApi's Google Flights
API](https://serpapi.com/google-flights-api), which returns structured
itineraries including flight number, aircraft type, cabin, and price. Sign up
at **<https://serpapi.com/users/sign_up>**.

As of August 2026 the free plan includes:

| | Free plan |
|---|---|
| Searches | **250 per month** |
| Throughput | 50 per hour |
| Cost | $0, no time limit |
| Paid tiers | from $25/mo for 1,000 searches |

Unused searches don't roll over; the allowance resets monthly. Check current
terms at <https://serpapi.com/pricing> — this tier was raised from 100 to 250
in 2026, so the numbers above may have moved again.

The hourly throughput cap is the one that bites in practice. A sweep costs
`dates x cabins x party sizes` searches, so a 10-date, 3-party pull is 30
requests per direction — fine. A 20-date sweep at 3 party sizes is 60 and will
hit the hourly ceiling partway through. `fetch_flights.py` paces itself at one
request per second and retries transient failures, but it won't wait out an
hourly limit; split large pulls into separate runs.

You can check your remaining balance any time:

```bash
curl -s "https://serpapi.com/account?api_key=$(cat SERPAPI_KEY.txt)" \
  | python3 -m json.tool | grep -E 'usage|left'
```

### Storing the key

Put it in `SERPAPI_KEY.txt` beside the scripts — that filename is gitignored.
Alternatively set `SERPAPI_KEY` in the environment, or point `--key-file`
anywhere you like. The key is never written to the output CSVs.

## Usage

### 1. Fetch fares

```bash
uv run python fetch_flights.py JFK SFO \
    --start 2026-11-20 --end 2026-11-25 \
    --cabin economy --max-stops 0 --adults 3 --sweep
```

Writes `data/jfk_sfo_2026-11-20-2026-11-25_3pax.csv`.

`--sweep` is the important flag: it queries party sizes 1 through `--adults`,
which is what makes bucket detection possible. Without it you get one price
per flight and no way to know how deep the bucket goes.

Cost is `dates x cabins x party sizes` searches. The example above is 18.

| Flag | Meaning |
|------|---------|
| `--cabin` | `economy`, `premium-economy`, `business`, `first` (multiple allowed) |
| `--max-stops` | `0` for nonstop only |
| `--max-price` | per-person cap; deferred to analysis when sweeping |
| `--adults` | party size |
| `--sweep` | query every party size from 1 to `--adults` |
| `--key-file` | alternate path to the API key |
| `--output` | override the auto-generated path; `-` for stdout |

### 2. Inspect a day

```bash
uv run python summarize.py data/sfo_jfk_2026-11-27-2026-12-02_3pax.csv \
    --parties 2 3 --max-price 500
```

One price column per party size. An asterisk means separate reservations win.

### 3. Rank trips

```bash
uv run python analyze_trips.py \
    data/jfk_sfo_2026-11-20-2026-11-25_3pax.csv \
    data/sfo_jfk_2026-11-27-2026-12-02_3pax.csv \
    --full-days 6 --passengers 3 --max-price 500 \
    --must-include 2026-11-26 --same-day-outbound
```

Against the bundled data that returns:

```
6 FULL DAYS AT DESTINATION
--------------------------------------------------------------------------
  1. $432/person  |  $1,296 for 3
     Out   Tue Nov 24  JetBlue B6 1515  06:35 / 10:09  Airbus A320  Economy  $180
     Back  Tue Dec 1  Alaska AS 30  06:18 / 15:03  Boeing 737  Economy  $252

  2. $449/person  |  $1,347 for 3  [book seats separately, saves $3]
     Out   Wed Nov 25  Alaska AS 41  20:29 / 23:59  Boeing 737  Economy  $269
     Back  Wed Dec 2  Alaska AS 1543  09:05 / 17:49  Boeing 737  Economy  $180*
```

A **full day** is one spent entirely at the destination, so both travel days
are excluded: `return_date = departure_date + full_days + 1`. Pass several
values to `--full-days` to compare trip lengths side by side.

`--must-include` requires the stay to contain a date — useful for anchoring a
trip around a holiday.

| Flag | Meaning |
|------|---------|
| `--full-days` | day counts to evaluate |
| `--passengers` | party size |
| `--must-include` | date the stay must contain |
| `--depart-from` | earliest acceptable outbound date |
| `--same-day-outbound` | outbound must land the same calendar day |
| `--same-day-return` | reject red-eye returns |
| `--latest-outbound-arrival` | arrival cutoff, `HH:MM` |
| `--earliest-return-departure` | departure cutoff, `HH:MM` |
| `--options` | how many to show per scenario |
| `--dump` | also print every qualifying flight by day |

## Caveats

- **Separate reservations mean separate records.** If a flight cancels, the
  airline treats your party as unrelated passengers with independent
  rebooking. Weigh that against the saving — in the bundled data it ranges
  from $3 to $430.
- **The split price assumes no repricing** between bookings. Inventory is
  live; transactions seconds apart usually hold, but nothing guarantees it.
- **Each direction is priced as a one-way and summed.** Standard for domestic
  US itineraries, but worth spot-checking against a round-trip search on
  routes where that doesn't hold.
- **Fares are snapshots.** Holiday pricing moves daily.

The `data/` directory holds real JFK/SFO fares so you can try `summarize.py`
and `analyze_trips.py` without an API key.

## License

MIT
